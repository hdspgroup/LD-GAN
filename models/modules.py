import torch
from torch import nn

import torch.nn.functional as F
from torch.nn import Parameter as P


# Projection of x onto y

def proj(x, y):
    return torch.mm(y, x.t()) * y / torch.mm(y, y.t())


# Orthogonalize x wrt list of vectors ys

def gram_schmidt(x, ys):
    for y in ys:
        x = x - proj(x, y)
    return x


# Apply num_itrs steps of the power method to estimate top N singular values.

def power_iteration(W, u_, update=True, eps=1e-12):
    # Lists holding singular vectors and values
    us, vs, svs = [], [], []
    for i, u in enumerate(u_):
        # Run one step of the power iteration
        with torch.no_grad():
            v = torch.matmul(u, W)
            # Run Gram-Schmidt to subtract components of all other singular vectors
            v = F.normalize(gram_schmidt(v, vs), eps=eps)
            # Add to the list
            vs += [v]
            # Update the other singular vector
            u = torch.matmul(v, W.t())
            # Run Gram-Schmidt to subtract components of all other singular vectors
            u = F.normalize(gram_schmidt(u, us), eps=eps)
            # Add to the list
            us += [u]
            if update:
                u_[i][:] = u
        # Compute this singular value and add it to the list
        svs += [torch.squeeze(torch.matmul(torch.matmul(v, W.t()), u.t()))]
        # svs += [torch.sum(F.linear(u, W.transpose(0, 1)) * v)]
    return svs, us, vs


# Spectral normalization base class

class SN(object):
    def __init__(self, num_svs, num_itrs, num_outputs, transpose=False, eps=1e-12):
        self.num_itrs = num_itrs
        self.num_svs = num_svs
        self.transpose = transpose
        self.eps = eps

        # Register a singular vector for each sv
        for i in range(self.num_svs):
            self.register_buffer('u%d' % i, torch.randn(1, num_outputs))
            self.register_buffer('sv%d' % i, torch.ones(1))

    # Singular vectors (u side)
    @property
    def u(self):
        return [getattr(self, 'u%d' % i) for i in range(self.num_svs)]

    # Singular values;
    # note that these buffers are just for logging and are not used in training.
    @property
    def sv(self):
        return [getattr(self, 'sv%d' % i) for i in range(self.num_svs)]

    # Compute the spectrally-normalized weight
    def W_(self):
        W_mat = self.weight.view(self.weight.size(0), -1)
        if self.transpose:
            W_mat = W_mat.t()
        # Apply num_itrs power iterations
        for _ in range(self.num_itrs):
            svs, us, vs = power_iteration(W_mat, self.u, update=self.training, eps=self.eps)
        # Update the svs
        if self.training:
            with torch.no_grad():  # Make sure to do this in a no_grad() context or you'll get memory leaks!
                for i, sv in enumerate(svs):
                    self.sv[i][:] = sv
        return self.weight / svs[0]


# 2D Conv layer with spectral norm

class SNConv2d(nn.Conv2d, SN):
    def __init__(self, in_channels, out_channels, kernel_size, stride=1,
                 padding=0, dilation=1, groups=1, bias=True,
                 num_svs=1, num_itrs=1, eps=1e-12):
        nn.Conv2d.__init__(self, in_channels, out_channels, kernel_size, stride,
                           padding, dilation, groups, bias)
        SN.__init__(self, num_svs, num_itrs, out_channels, eps=eps)

    def forward(self, x):
        return F.conv2d(x, self.W_(), self.bias, self.stride,
                        self.padding, self.dilation, self.groups)


# Linear layer with spectral norm

class SNLinear(nn.Linear, SN):
    def __init__(self, in_features, out_features, bias=True, num_svs=1, num_itrs=1, eps=1e-12):
        nn.Linear.__init__(self, in_features, out_features, bias)
        SN.__init__(self, num_svs, num_itrs, out_features, eps=eps)

    def forward(self, x):
        return F.linear(x, self.W_(), self.bias)


# Embedding layer with spectral norm

class SNEmbedding(nn.Embedding, SN):
    def __init__(self, num_embeddings, embedding_dim, padding_idx=None, max_norm=None, norm_type=2,
                 scale_grad_by_freq=False, sparse=False, _weight=None, num_svs=1, num_itrs=1, eps=1e-12):
        nn.Embedding.__init__(self, num_embeddings, embedding_dim, padding_idx, max_norm, norm_type,
                              scale_grad_by_freq, sparse, _weight)
        SN.__init__(self, num_svs, num_itrs, num_embeddings, eps=eps)

    def forward(self, x):
        return F.embedding(x, self.W_())


# ----------------------- Batch Norms ----------------------- #

def group_norm(x, norm_style):
    if 'ch' in norm_style:
        ch = int(norm_style.split('_')[-1])
        groups = max(int(x.shape[1]) // ch, 1)

    elif 'grp' in norm_style:
        groups = int(norm_style.split('_')[-1])

    else:
        groups = 16

    return F.group_norm(x, groups)


# Normal

class BN(nn.Module):
    def __init__(self, output_size, eps=1e-5, momentum=0.1, cross_replica=False):
        super(BN, self).__init__()
        self.output_size = output_size

        # Prepare gain and bias layers

        self.gain = P(torch.ones(output_size), requires_grad=True)
        self.bias = P(torch.zeros(output_size), requires_grad=True)

        self.eps = eps  # epsilon to avoid dividing by 0
        self.momentum = momentum
        self.cross_replica = cross_replica

        self.register_buffer('stored_mean', torch.zeros(output_size))
        self.register_buffer('stored_var', torch.ones(output_size))

    def forward(self, x, y=None):
        return F.batch_norm(x, self.stored_mean, self.stored_var, self.gain, self.bias,
                            self.training, self.momentum, self.eps)


# Class-conditional bn

class CCBN(nn.Module):
    def __init__(self, output_size, input_size, which_linear, eps=1e-5, momentum=0.1, cross_replica=False,
                 norm_style='bn'):
        super(CCBN, self).__init__()
        self.output_size, self.input_size = output_size, input_size

        # Prepare gain and bias layers

        self.gain = which_linear(input_size, output_size)
        self.bias = which_linear(input_size, output_size)

        self.eps = eps  # epsilon to avoid dividing by 0
        self.momentum = momentum
        self.cross_replica = cross_replica
        self.norm_style = norm_style

        self.register_buffer('stored_mean', torch.zeros(output_size))
        self.register_buffer('stored_var', torch.ones(output_size))

    def forward(self, x, y):
        # Calculate class-conditional gains and biases

        gain = (1 + self.gain(y)).view(y.size(0), -1, 1, 1)
        bias = self.bias(y).view(y.size(0), -1, 1, 1)

        if self.norm_style == 'bn':
            out = F.batch_norm(x, self.stored_mean, self.stored_var, None, None, self.training, 0.1, self.eps)

        elif self.norm_style == 'in':
            out = F.instance_norm(x, self.stored_mean, self.stored_var, None, None, self.training, 0.1, self.eps)

        elif self.norm_style == 'gn':
            out = group_norm(x, self.normstyle)

        elif self.norm_style == 'nonorm':
            out = x

        else:
            raise ValueError('Unknown normalization style')

        return out * gain + bias

    def extra_repr(self):
        s = 'out: {output_size}, in: {input_size},'
        s += ' cross_replica={cross_replica}'
        return s.format(**self.__dict__)


# ----------------------- Generator block ----------------------- #


class GBlock(nn.Module):
    def __init__(self, in_channels, out_channels, which_conv=nn.Conv2d, which_bn=BN, activation=None, upsample=None):
        super(GBlock, self).__init__()

        self.in_channels, self.out_channels = in_channels, out_channels
        self.which_conv, self.which_bn = which_conv, which_bn
        self.activation = activation
        self.upsample = upsample

        # Conv layers

        self.conv1 = self.which_conv(self.in_channels, self.out_channels)
        self.conv2 = self.which_conv(self.out_channels, self.out_channels)

        self.learnable_sc = in_channels != out_channels or upsample
        if self.learnable_sc:
            self.conv_sc = self.which_conv(in_channels, out_channels, kernel_size=1, padding=0)

        # Batchnorm layers

        self.bn1 = self.which_bn(in_channels)
        self.bn2 = self.which_bn(out_channels)

        # upsample layers

        self.upsample = upsample

    def forward(self, x, y):
        h = self.activation(self.bn1(x, y))

        if self.upsample:
            h = self.upsample(h)
            x = self.upsample(x)

        h = self.conv1(h)
        h = self.activation(self.bn2(h, y))
        h = self.conv2(h)

        if self.learnable_sc:
            x = self.conv_sc(x)

        return h + x


# ----------------------- Discriminator block ----------------------- #


class DBlock(nn.Module):
    def __init__(self, in_channels, out_channels, which_conv=SNConv2d, wide=True, preactivation=False, activation=None,
                 downsample=None, ):
        super(DBlock, self).__init__()

        self.in_channels, self.out_channels = in_channels, out_channels

        self.hidden_channels = self.out_channels if wide else self.in_channels
        self.which_conv = which_conv
        self.preactivation = preactivation
        self.activation = activation
        self.downsample = downsample

        # Conv layers

        self.conv1 = self.which_conv(self.in_channels, self.hidden_channels)
        self.conv2 = self.which_conv(self.hidden_channels, self.out_channels)

        self.learnable_sc = True if (in_channels != out_channels) or downsample else False
        if self.learnable_sc:
            self.conv_sc = self.which_conv(in_channels, out_channels, kernel_size=1, padding=0)

    def shortcut(self, x):
        if self.preactivation:
            if self.learnable_sc:
                x = self.conv_sc(x)

            if self.downsample:
                x = self.downsample(x)

        else:
            if self.downsample:
                x = self.downsample(x)

            if self.learnable_sc:
                x = self.conv_sc(x)

        return x

    def forward(self, x):
        if self.preactivation:
            h = F.relu(x)

        else:
            h = x

        h = self.conv1(h)
        h = self.conv2(self.activation(h))

        if self.downsample:
            h = self.downsample(h)

        return h + self.shortcut(x)
