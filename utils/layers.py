import torch.nn.functional as F

from utils.general import *

import torch
from torch import nn
from timm.models.layers import ConvBnAct, DropPath, create_conv2d, create_act_layer, LayerNorm2d, GroupNorm, SeparableConv2d
import functools

try:
    from mish_cuda import MishCuda as Mish
    
except:
    class Mish(nn.Module):  # https://github.com/digantamisra98/Mish
        def forward(self, x):
            return x * F.softplus(x).tanh()
    
try:
    from pytorch_wavelets import DWTForward, DWTInverse

    class DWT(nn.Module):
        def __init__(self):
            super(DWT, self).__init__()
            self.xfm = DWTForward(J=1, wave='db1', mode='zero')

        def forward(self, x):
            b,c,w,h = x.shape
            yl, yh = self.xfm(x)
            return torch.cat([yl/2., yh[0].view(b,-1,w//2,h//2)/2.+.5], 1)
        
except: # using Reorg instead
    class DWT(nn.Module):
        def forward(self, x):
            return torch.cat([x[..., ::2, ::2], x[..., 1::2, ::2], x[..., ::2, 1::2], x[..., 1::2, 1::2]], 1)

class LPU(nn.Module):
    def __init__(self, dim, kernel_size=3, stride=1, padding=1, act_layer="swish"):
        super().__init__()

        self.dwConv = create_conv2d(in_channels=dim, out_channels=dim, kernel_size=kernel_size,
                                    stride=stride, padding=padding, depthwise=True)
        self.act_layer = create_act_layer(act_layer)

    def forward(self, x):
        x = x + self.act_layer(self.dwConv(x))
        return x

class DLA(nn.Module):
    def __init__(self, dim, numGroups, expansion_ratio, act_layer="swish"):
        super().__init__()
        hidden_dim = int(dim * expansion_ratio)
        expansion_ratio = int(expansion_ratio)
        self.expand = ConvBnAct(in_channels=dim, out_channels=hidden_dim, kernel_size=1,
                                stride=1, padding=0, apply_act=True, act_layer=act_layer,
                                norm_layer=functools.partial(GroupNorm, num_groups=expansion_ratio))
        self.dwConv = ConvBnAct(in_channels=hidden_dim, out_channels=hidden_dim, kernel_size=3,
                                stride=1, padding=1, apply_act=True, act_layer=act_layer, groups=hidden_dim,
                                norm_layer=functools.partial(GroupNorm, num_groups=expansion_ratio))
        self.reduce = ConvBnAct(in_channels=hidden_dim, out_channels=dim, kernel_size=1,
                                stride=1, padding=0, apply_act=False,
                                norm_layer=functools.partial(GroupNorm, num_groups=1)) # used numGroups to get 66.5 imagenet val

    def forward(self, x):
        x = self.expand(x)
        x = self.dwConv(x)
        x = self.reduce(x)
        return x

class LMHSA(nn.Module):
    def __init__(self, patchSize, dim, k, attn_expansion=3, num_heads=8, qkv_bias=False, qk_scale=None, attn_drop=0.,
                 proj_drop=0.):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = qk_scale or head_dim ** -0.5

        self.relative_pos_bias = nn.Parameter(torch.ones(patchSize ** 2, (patchSize ** 2) // (k ** 2)))
        self.q = nn.Linear(dim, dim, bias=qkv_bias)
        self.down = create_conv2d(in_channels=dim, out_channels=dim, kernel_size=k, stride=k, padding=0, depthwise=True,
                                  bias=qkv_bias)
        self.kv = nn.Linear(dim, dim * 2, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)
        self.DLA = DLA(self.num_heads, self.num_heads, attn_expansion)

    def forward(self, x):
        B, C, H, W = x.shape
        N = H*W
        kv = self.down(x).reshape(B, C, -1).permute(0,2,1)
        kv = self.kv(kv)
        kv = kv.reshape(B, kv.shape[1], 2, self.num_heads, C//self.num_heads).permute(2, 0, 3, 1, 4)
        x = self.q(x.reshape(B, C, -1).permute(0,2,1)).reshape(B, N, self.num_heads, C//self.num_heads).permute(0, 2, 1, 3)
        k, v = kv[0], kv[1]

        attn = (x @ k.transpose(-2, -1)) * self.scale + self.relative_pos_bias
        attn = attn.softmax(dim=-1)
        attn = self.attn_drop(attn)
        attn = self.DLA(attn)
        x = (attn @ v).transpose(1, 2).reshape(B, N, C)
        x = self.proj(x)
        x = self.proj_drop(x)
        x = x.reshape(B, C, H, W)
        return x #, attn

class IFFN(nn.Module):
    def __init__(self, dim, proj_ratio=4, act_layer="swish"):
        super().__init__()
        out_dim = int(proj_ratio*dim)
        normGroups = out_dim//dim
        self.conv0 = ConvBnAct(in_channels=dim, out_channels=out_dim, kernel_size=1, stride=1,
                               padding=0, apply_act=False,
                               norm_layer=functools.partial(GroupNorm, num_groups=1))
        self.conv1 = ConvBnAct(in_channels=out_dim, out_channels=out_dim, kernel_size=3, stride=1,
                               padding=1, groups=out_dim, apply_act=True, act_layer=act_layer,
                               norm_layer=functools.partial(GroupNorm, num_groups=normGroups))
        self.conv2 = ConvBnAct(in_channels=out_dim, out_channels=dim, kernel_size=1, stride=1, padding=0,
                               apply_act=True, act_layer=act_layer,
                               norm_layer=functools.partial(GroupNorm, num_groups=1))

    def forward(self, x):
        x = self.conv0(x)
        x = x + self.conv1(x)
        x = self.conv2(x)
        return x

class CMTBlock(nn.Module):
    def __init__(self, patchSize, dim, heads, reduction, expansion, drop_rate=0., attn_drop_rate=0., drop_path_rate=0.,
                 act_layer="swish"):
        super().__init__()

        self.lpu = LPU(dim)
        self.norm0 = GroupNorm(dim, num_groups=heads)
        self.mhsa = LMHSA(patchSize, dim, k=reduction, num_heads=heads, attn_drop=attn_drop_rate, proj_drop=drop_rate)
        self.norm1 = GroupNorm(dim, num_groups=heads)
        self.iffn = IFFN(dim, expansion, act_layer=act_layer)
        self.drop_path = DropPath(drop_path_rate) if drop_path_rate > 0. else nn.Identity()
        self.act_layer = create_act_layer(act_layer)

    def forward(self, x):
        x = self.lpu(x)
        x = x + self.drop_path(self.act_layer(self.mhsa(self.norm0(x))))
        x = x + self.iffn(self.norm1(x))
        return x


class Reorg(nn.Module):
    def forward(self, x):
        return torch.cat([x[..., ::2, ::2], x[..., 1::2, ::2], x[..., ::2, 1::2], x[..., 1::2, 1::2]], 1)


def make_divisible(v, divisor):
    # Function ensures all layers have a channel number that is divisible by 8
    # https://github.com/tensorflow/models/blob/master/research/slim/nets/mobilenet/mobilenet.py
    return math.ceil(v / divisor) * divisor


class Flatten(nn.Module):
    # Use after nn.AdaptiveAvgPool2d(1) to remove last 2 dimensions
    def forward(self, x):
        return x.view(x.size(0), -1)


class Concat(nn.Module):
    # Concatenate a list of tensors along dimension
    def __init__(self, dimension=1):
        super(Concat, self).__init__()
        self.d = dimension

    def forward(self, x):
        return torch.cat(x, self.d)

class LMHA(nn.Module):
    def __init__(self, patchSize, dim, reduction=1, k=2, attn_expansion=3, num_heads=8, qkv_bias=False, qk_scale=None, attn_drop=0.,
                 proj_drop=0.):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = qk_scale or head_dim ** -0.5

        self.relative_pos_bias = nn.Parameter(torch.ones(patchSize ** 2, ((patchSize // k) ** 2) // (reduction ** 2)))
        self.q = nn.Linear(dim, dim, bias=qkv_bias)

        self.down = None
        if reduction > 1:
            self.down = create_conv2d(in_channels=dim, out_channels=dim, kernel_size=reduction, stride=reduction, padding=0,
                                      depthwise=True, bias=qkv_bias)

        self.kv = nn.Linear(dim, dim * 2, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)
        self.DLA = DLA(self.num_heads, self.num_heads, attn_expansion)

    def forward(self, q, kv):
        B, C, H, W = q.shape
        N = H * W

        if self.down:
            kv = self.down(kv)

        kv = kv.reshape(B, kv.shape[1], -1).permute(0, 2, 1)
        kv = self.kv(kv)
        kv = kv.reshape(B, -1, 2, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
        q = self.q(q.reshape(B, C, -1).permute(0, 2, 1)).reshape(B, N, self.num_heads, C // self.num_heads).permute(0, 2, 1, 3)
        k, v = kv[0], kv[1]

        attn = (q @ k.transpose(-2, -1)) * self.scale + self.relative_pos_bias
        attn = attn.softmax(dim=-1)
        attn = self.attn_drop(attn)
        attn = self.DLA(attn)
        q = (attn @ v).transpose(1, 2).reshape(B, N, C)
        q = self.proj(q)
        q = self.proj_drop(q)
        q = q.reshape(B, C, H, W)
        return q  # , attn

# TODO: implement for concatenating features from different levels
class LevelFeatureAttn(nn.Module):
    def __init__(self, patchSize, layers, dim, heads, expansion, reduction=1, drop_rate=0., attn_drop_rate=0., drop_path_rate=0.,
                 act_layer="swish"):
        super().__init__()

        self.layers = layers
        assert len(layers) == 2, "Route attention needs 1 input for Q and 1 input for KV"
        self.mha = LMHA(patchSize, dim, reduction=reduction, num_heads=heads, attn_drop=attn_drop_rate, proj_drop=drop_rate)
        self.norm1 = GroupNorm(dim, num_groups=heads)
        self.iffn = IFFN(dim, expansion, act_layer=act_layer)
        self.drop_path = DropPath(drop_path_rate) if drop_path_rate > 0. else nn.Identity()
        self.act_layer = create_act_layer(act_layer)

    def forward(self, _, outputs):
        qIdx, kvIdx = self.layers
        outputs = outputs[qIdx] + self.drop_path(self.act_layer(self.mha(outputs[qIdx], outputs[kvIdx])))
        outputs = outputs + self.iffn(self.norm1(outputs))
        return outputs


class FeatureConcat(nn.Module):
    def __init__(self, layers):
        super(FeatureConcat, self).__init__()
        self.layers = layers  # layer indices
        self.multiple = len(layers) > 1  # multiple layers flag

    def forward(self, x, outputs):
        return torch.cat([outputs[i] for i in self.layers], 1) if self.multiple else outputs[self.layers[0]]


class FeatureConcat2(nn.Module):
    def __init__(self, layers):
        super(FeatureConcat2, self).__init__()
        self.layers = layers  # layer indices
        self.multiple = len(layers) > 1  # multiple layers flag

    def forward(self, x, outputs):
        return torch.cat([outputs[self.layers[0]], outputs[self.layers[1]].detach()], 1)


class FeatureConcat3(nn.Module):
    def __init__(self, layers):
        super(FeatureConcat3, self).__init__()
        self.layers = layers  # layer indices
        self.multiple = len(layers) > 1  # multiple layers flag

    def forward(self, x, outputs):
        return torch.cat([outputs[self.layers[0]], outputs[self.layers[1]].detach(), outputs[self.layers[2]].detach()], 1)


class FeatureConcat_l(nn.Module):
    def __init__(self, layers):
        super(FeatureConcat_l, self).__init__()
        self.layers = layers  # layer indices
        self.multiple = len(layers) > 1  # multiple layers flag

    def forward(self, x, outputs):
        return torch.cat([outputs[i][:,:outputs[i].shape[1]//2,:,:] for i in self.layers], 1) if self.multiple else outputs[self.layers[0]][:,:outputs[self.layers[0]].shape[1]//2,:,:]


class WeightedFeatureFusion(nn.Module):  # weighted sum of 2 or more layers https://arxiv.org/abs/1911.09070
    def __init__(self, layers, weight=False):
        super(WeightedFeatureFusion, self).__init__()
        self.layers = layers  # layer indices
        self.weight = weight  # apply weights boolean
        self.n = len(layers) + 1  # number of layers
        if weight:
            self.w = nn.Parameter(torch.zeros(self.n), requires_grad=True)  # layer weights

    def forward(self, x, outputs):
        # Weights
        if self.weight:
            w = torch.sigmoid(self.w) * (2 / self.n)  # sigmoid weights (0-1)
            x = x * w[0]

        # Fusion
        nx = x.shape[1]  # input channels
        for i in range(self.n - 1):
            a = outputs[self.layers[i]] * w[i + 1] if self.weight else outputs[self.layers[i]]  # feature to add
            na = a.shape[1]  # feature channels

            # Adjust channels
            if nx == na:  # same shape
                x = x + a
            elif nx > na:  # slice input
                x[:, :na] = x[:, :na] + a  # or a = nn.ZeroPad2d((0, 0, 0, 0, 0, dc))(a); x = x + a
            else:  # slice feature
                x = x + a[:, :nx]

        return x


class MixConv2d(nn.Module):  # MixConv: Mixed Depthwise Convolutional Kernels https://arxiv.org/abs/1907.09595
    def __init__(self, in_ch, out_ch, k=(3, 5, 7), stride=1, dilation=1, bias=True, method='equal_params'):
        super(MixConv2d, self).__init__()

        groups = len(k)
        if method == 'equal_ch':  # equal channels per group
            i = torch.linspace(0, groups - 1E-6, out_ch).floor()  # out_ch indices
            ch = [(i == g).sum() for g in range(groups)]
        else:  # 'equal_params': equal parameter count per group
            b = [out_ch] + [0] * groups
            a = np.eye(groups + 1, groups, k=-1)
            a -= np.roll(a, 1, axis=1)
            a *= np.array(k) ** 2
            a[0] = 1
            ch = np.linalg.lstsq(a, b, rcond=None)[0].round().astype(int)  # solve for equal weight indices, ax = b

        self.m = nn.ModuleList([nn.Conv2d(in_channels=in_ch,
                                          out_channels=ch[g],
                                          kernel_size=k[g],
                                          stride=stride,
                                          padding=k[g] // 2,  # 'same' pad
                                          dilation=dilation,
                                          bias=bias) for g in range(groups)])

    def forward(self, x):
        return torch.cat([m(x) for m in self.m], 1)


# Activation functions below -------------------------------------------------------------------------------------------
class SwishImplementation(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x):
        ctx.save_for_backward(x)
        return x * torch.sigmoid(x)

    @staticmethod
    def backward(ctx, grad_output):
        x = ctx.saved_tensors[0]
        sx = torch.sigmoid(x)  # sigmoid(ctx)
        return grad_output * (sx * (1 + x * (1 - sx)))


class MishImplementation(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x):
        ctx.save_for_backward(x)
        return x.mul(torch.tanh(F.softplus(x)))  # x * tanh(ln(1 + exp(x)))

    @staticmethod
    def backward(ctx, grad_output):
        x = ctx.saved_tensors[0]
        sx = torch.sigmoid(x)
        fx = F.softplus(x).tanh()
        return grad_output * (fx + x * sx * (1 - fx * fx))


class MemoryEfficientSwish(nn.Module):
    def forward(self, x):
        return SwishImplementation.apply(x)


class MemoryEfficientMish(nn.Module):
    def forward(self, x):
        return MishImplementation.apply(x)


class Swish(nn.Module):
    def forward(self, x):
        return x * torch.sigmoid(x)


class HardSwish(nn.Module):  # https://arxiv.org/pdf/1905.02244.pdf
    def forward(self, x):
        return x * F.hardtanh(x + 3, 0., 6., True) / 6.


class DeformConv2d(nn.Module):
    def __init__(self, inc, outc, kernel_size=3, padding=1, stride=1, bias=None, modulation=False):
        """
        Args:
            modulation (bool, optional): If True, Modulated Defomable Convolution (Deformable ConvNets v2).
        """
        super(DeformConv2d, self).__init__()
        self.kernel_size = kernel_size
        self.padding = padding
        self.stride = stride
        self.zero_padding = nn.ZeroPad2d(padding)
        self.conv = nn.Conv2d(inc, outc, kernel_size=kernel_size, stride=kernel_size, bias=bias)

        self.p_conv = nn.Conv2d(inc, 2*kernel_size*kernel_size, kernel_size=3, padding=1, stride=stride)
        nn.init.constant_(self.p_conv.weight, 0)
        self.p_conv.register_backward_hook(self._set_lr)

        self.modulation = modulation
        if modulation:
            self.m_conv = nn.Conv2d(inc, kernel_size*kernel_size, kernel_size=3, padding=1, stride=stride)
            nn.init.constant_(self.m_conv.weight, 0)
            self.m_conv.register_backward_hook(self._set_lr)

    @staticmethod
    def _set_lr(module, grad_input, grad_output):
        grad_input = (grad_input[i] * 0.1 for i in range(len(grad_input)))
        grad_output = (grad_output[i] * 0.1 for i in range(len(grad_output)))

    def forward(self, x):
        offset = self.p_conv(x)
        if self.modulation:
            m = torch.sigmoid(self.m_conv(x))

        dtype = offset.data.type()
        ks = self.kernel_size
        N = offset.size(1) // 2

        if self.padding:
            x = self.zero_padding(x)

        # (b, 2N, h, w)
        p = self._get_p(offset, dtype)

        # (b, h, w, 2N)
        p = p.contiguous().permute(0, 2, 3, 1)
        q_lt = p.detach().floor()
        q_rb = q_lt + 1

        q_lt = torch.cat([torch.clamp(q_lt[..., :N], 0, x.size(2)-1), torch.clamp(q_lt[..., N:], 0, x.size(3)-1)], dim=-1).long()
        q_rb = torch.cat([torch.clamp(q_rb[..., :N], 0, x.size(2)-1), torch.clamp(q_rb[..., N:], 0, x.size(3)-1)], dim=-1).long()
        q_lb = torch.cat([q_lt[..., :N], q_rb[..., N:]], dim=-1)
        q_rt = torch.cat([q_rb[..., :N], q_lt[..., N:]], dim=-1)

        # clip p
        p = torch.cat([torch.clamp(p[..., :N], 0, x.size(2)-1), torch.clamp(p[..., N:], 0, x.size(3)-1)], dim=-1)

        # bilinear kernel (b, h, w, N)
        g_lt = (1 + (q_lt[..., :N].type_as(p) - p[..., :N])) * (1 + (q_lt[..., N:].type_as(p) - p[..., N:]))
        g_rb = (1 - (q_rb[..., :N].type_as(p) - p[..., :N])) * (1 - (q_rb[..., N:].type_as(p) - p[..., N:]))
        g_lb = (1 + (q_lb[..., :N].type_as(p) - p[..., :N])) * (1 - (q_lb[..., N:].type_as(p) - p[..., N:]))
        g_rt = (1 - (q_rt[..., :N].type_as(p) - p[..., :N])) * (1 + (q_rt[..., N:].type_as(p) - p[..., N:]))

        # (b, c, h, w, N)
        x_q_lt = self._get_x_q(x, q_lt, N)
        x_q_rb = self._get_x_q(x, q_rb, N)
        x_q_lb = self._get_x_q(x, q_lb, N)
        x_q_rt = self._get_x_q(x, q_rt, N)

        # (b, c, h, w, N)
        x_offset = g_lt.unsqueeze(dim=1) * x_q_lt + \
                   g_rb.unsqueeze(dim=1) * x_q_rb + \
                   g_lb.unsqueeze(dim=1) * x_q_lb + \
                   g_rt.unsqueeze(dim=1) * x_q_rt

        # modulation
        if self.modulation:
            m = m.contiguous().permute(0, 2, 3, 1)
            m = m.unsqueeze(dim=1)
            m = torch.cat([m for _ in range(x_offset.size(1))], dim=1)
            x_offset *= m

        x_offset = self._reshape_x_offset(x_offset, ks)
        out = self.conv(x_offset)

        return out

    def _get_p_n(self, N, dtype):
        p_n_x, p_n_y = torch.meshgrid(
            torch.arange(-(self.kernel_size-1)//2, (self.kernel_size-1)//2+1),
            torch.arange(-(self.kernel_size-1)//2, (self.kernel_size-1)//2+1))
        # (2N, 1)
        p_n = torch.cat([torch.flatten(p_n_x), torch.flatten(p_n_y)], 0)
        p_n = p_n.view(1, 2*N, 1, 1).type(dtype)

        return p_n

    def _get_p_0(self, h, w, N, dtype):
        p_0_x, p_0_y = torch.meshgrid(
            torch.arange(1, h*self.stride+1, self.stride),
            torch.arange(1, w*self.stride+1, self.stride))
        p_0_x = torch.flatten(p_0_x).view(1, 1, h, w).repeat(1, N, 1, 1)
        p_0_y = torch.flatten(p_0_y).view(1, 1, h, w).repeat(1, N, 1, 1)
        p_0 = torch.cat([p_0_x, p_0_y], 1).type(dtype)

        return p_0

    def _get_p(self, offset, dtype):
        N, h, w = offset.size(1)//2, offset.size(2), offset.size(3)

        # (1, 2N, 1, 1)
        p_n = self._get_p_n(N, dtype)
        # (1, 2N, h, w)
        p_0 = self._get_p_0(h, w, N, dtype)
        p = p_0 + p_n + offset
        return p

    def _get_x_q(self, x, q, N):
        b, h, w, _ = q.size()
        padded_w = x.size(3)
        c = x.size(1)
        # (b, c, h*w)
        x = x.contiguous().view(b, c, -1)

        # (b, h, w, N)
        index = q[..., :N]*padded_w + q[..., N:]  # offset_x*w + offset_y
        # (b, c, h*w*N)
        index = index.contiguous().unsqueeze(dim=1).expand(-1, c, -1, -1, -1).contiguous().view(b, c, -1)

        x_offset = x.gather(dim=-1, index=index).contiguous().view(b, c, h, w, N)

        return x_offset

    @staticmethod
    def _reshape_x_offset(x_offset, ks):
        b, c, h, w, N = x_offset.size()
        x_offset = torch.cat([x_offset[..., s:s+ks].contiguous().view(b, c, h, w*ks) for s in range(0, N, ks)], dim=-1)
        x_offset = x_offset.contiguous().view(b, c, h*ks, w*ks)

        return x_offset
    
    
class GAP(nn.Module):
    def __init__(self):
        super(GAP, self).__init__()
        self.avg_pool = nn.AdaptiveAvgPool2d(1)
    def forward(self, x):
        #b, c, _, _ = x.size()        
        return self.avg_pool(x)#.view(b, c)
    
    
class Silence(nn.Module):
    def __init__(self):
        super(Silence, self).__init__()
    def forward(self, x):    
        return x


class ScaleChannel(nn.Module):  # weighted sum of 2 or more layers https://arxiv.org/abs/1911.09070
    def __init__(self, layers):
        super(ScaleChannel, self).__init__()
        self.layers = layers  # layer indices

    def forward(self, x, outputs):
        a = outputs[self.layers[0]]
        return x.expand_as(a) * a


class ShiftChannel(nn.Module):  # weighted sum of 2 or more layers https://arxiv.org/abs/1911.09070
    def __init__(self, layers):
        super(ShiftChannel, self).__init__()
        self.layers = layers  # layer indices

    def forward(self, x, outputs):
        a = outputs[self.layers[0]]
        return a.expand_as(x) + x


class ShiftChannel2D(nn.Module):  # weighted sum of 2 or more layers https://arxiv.org/abs/1911.09070
    def __init__(self, layers):
        super(ShiftChannel2D, self).__init__()
        self.layers = layers  # layer indices

    def forward(self, x, outputs):
        a = outputs[self.layers[0]].view(1,-1,1,1)
        return a.expand_as(x) + x


class ControlChannel(nn.Module):  # weighted sum of 2 or more layers https://arxiv.org/abs/1911.09070
    def __init__(self, layers):
        super(ControlChannel, self).__init__()
        self.layers = layers  # layer indices

    def forward(self, x, outputs):
        a = outputs[self.layers[0]]
        return a.expand_as(x) * x


class ControlChannel2D(nn.Module):  # weighted sum of 2 or more layers https://arxiv.org/abs/1911.09070
    def __init__(self, layers):
        super(ControlChannel2D, self).__init__()
        self.layers = layers  # layer indices

    def forward(self, x, outputs):
        a = outputs[self.layers[0]].view(1,-1,1,1)
        return a.expand_as(x) * x


class AlternateChannel(nn.Module):  # weighted sum of 2 or more layers https://arxiv.org/abs/1911.09070
    def __init__(self, layers):
        super(AlternateChannel, self).__init__()
        self.layers = layers  # layer indices

    def forward(self, x, outputs):
        a = outputs[self.layers[0]]
        return torch.cat([a.expand_as(x), x], dim=1)


class AlternateChannel2D(nn.Module):  # weighted sum of 2 or more layers https://arxiv.org/abs/1911.09070
    def __init__(self, layers):
        super(AlternateChannel2D, self).__init__()
        self.layers = layers  # layer indices

    def forward(self, x, outputs):
        a = outputs[self.layers[0]].view(1,-1,1,1)
        return torch.cat([a.expand_as(x), x], dim=1)


class SelectChannel(nn.Module):  # weighted sum of 2 or more layers https://arxiv.org/abs/1911.09070
    def __init__(self, layers):
        super(SelectChannel, self).__init__()
        self.layers = layers  # layer indices

    def forward(self, x, outputs):
        a = outputs[self.layers[0]]
        return a.sigmoid().expand_as(x) * x


class SelectChannel2D(nn.Module):  # weighted sum of 2 or more layers https://arxiv.org/abs/1911.09070
    def __init__(self, layers):
        super(SelectChannel2D, self).__init__()
        self.layers = layers  # layer indices

    def forward(self, x, outputs):
        a = outputs[self.layers[0]].view(1,-1,1,1)
        return a.sigmoid().expand_as(x) * x


class ScaleSpatial(nn.Module):  # weighted sum of 2 or more layers https://arxiv.org/abs/1911.09070
    def __init__(self, layers):
        super(ScaleSpatial, self).__init__()
        self.layers = layers  # layer indices

    def forward(self, x, outputs):
        a = outputs[self.layers[0]]
        return x * a
    

class ImplicitA(nn.Module):
    def __init__(self, channel):
        super(ImplicitA, self).__init__()
        self.channel = channel
        self.implicit = nn.Parameter(torch.zeros(1, channel, 1, 1))
        nn.init.normal_(self.implicit, std=.02)

    def forward(self):
        return self.implicit


class ImplicitC(nn.Module):
    def __init__(self, channel):
        super(ImplicitC, self).__init__()
        self.channel = channel
        self.implicit = nn.Parameter(torch.zeros(1, channel, 1, 1))
        nn.init.normal_(self.implicit, std=.02)

    def forward(self):
        return self.implicit


class ImplicitM(nn.Module):
    def __init__(self, channel):
        super(ImplicitM, self).__init__()
        self.channel = channel
        self.implicit = nn.Parameter(torch.ones(1, channel, 1, 1))
        nn.init.normal_(self.implicit, mean=1., std=.02)

    def forward(self):
        return self.implicit
    


class Implicit2DA(nn.Module):
    def __init__(self, atom, channel):
        super(Implicit2DA, self).__init__()
        self.channel = channel
        self.implicit = nn.Parameter(torch.zeros(1, atom, channel, 1))
        nn.init.normal_(self.implicit, std=.02)

    def forward(self):
        return self.implicit


class Implicit2DC(nn.Module):
    def __init__(self, atom, channel):
        super(Implicit2DC, self).__init__()
        self.channel = channel
        self.implicit = nn.Parameter(torch.zeros(1, atom, channel, 1))
        nn.init.normal_(self.implicit, std=.02)

    def forward(self):
        return self.implicit


class Implicit2DM(nn.Module):
    def __init__(self, atom, channel):
        super(Implicit2DM, self).__init__()
        self.channel = channel
        self.implicit = nn.Parameter(torch.ones(1, atom, channel, 1))
        nn.init.normal_(self.implicit, mean=1., std=.02)

    def forward(self):
        return self.implicit
    
    
    