from functools import partial
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm

import torch
from torch import nn
from torch.nn import functional as F

from models.diff_aug import DiffAugment
from models.losses import loss_hinge_dis, loss_hinge_gen
from models.modules import SNLinear, GBlock, SNConv2d, CCBN, BN, SNEmbedding, DBlock
from scripts.spec2rgb import ColourSystem


# =#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#
# baseline
# =#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#

class LCGAN:
    def __init__(self, batch_size, latent_dim, num_classes, epochs, ema_losses, toggle_grads, in_channels, out_channels,
                 bands, down_samples, gen_lr, dis_lr, beta1, beta2, adam_eps, save_path, device):
        self.batch_size = batch_size
        self.latent_dim = latent_dim
        self.num_classes = num_classes
        self.bands = bands
        self.epochs = epochs
        self.ema_losses = ema_losses
        self.toggle_grads = toggle_grads
        self.writer = SummaryWriter(save_path)
        self.save_path = save_path
        self.device = device

        # ----------------- encoder & decoder ----------------- #

        self.encoder = self.build_encoder()
        self.decoder = self.build_decoder()

        for param in self.encoder.parameters():
            param.requires_grad = False

        for param in self.decoder.parameters():
            param.requires_grad = False

        # ----------------- generator & discriminator ----------------- #

        self.generator = self.build_generator(in_channels['gen'], out_channels['gen'], num_classes, latent_dim, bands,
                                              gen_lr, beta1, beta2, adam_eps)
        self.discriminator = self.build_discriminator(in_channels['dis'], out_channels['dis'], down_samples,
                                                      num_classes, dis_lr, beta1, beta2, adam_eps)

        self.gan = self.build_gan()

        compare = True if num_classes == 2 else False
        self.z, self.y = self.init_random_samples(compare=compare)
        self.fixed_z, self.fixed_y = self.init_random_samples(compare=compare)

        self.fixed_z.sample_()
        self.fixed_y.sample_()

        self.cs = ColourSystem(cs='sRGB', start=400, end=720, num=bands, device=device)

        if device == 'cuda' and torch.cuda.is_available():
            self.encoder.to(device)
            self.decoder.to(device)
            self.generator.to(device)
            self.discriminator.to(device)
            self.gan.to(device)
            print('Using GPU')

    def init_random_samples(self, compare=False):
        z = Distribution(torch.randn(self.batch_size['gen'], self.latent_dim, requires_grad=False))
        z.init_distribution('normal', mean=0.0, var=1.0)
        z = z.to(self.device, torch.float32)

        y = Distribution(torch.zeros(self.batch_size['gen'], requires_grad=False))
        y.init_distribution('comparison' if compare else 'categorical', num_categories=self.num_classes)
        y = y.to(self.device, torch.int64)

        return z, y

    def build_generator(self, in_channels, out_channels, num_classes, latent_dim, bands, lr, beta1, beta2, adam_eps):
        return Generator(in_channels, out_channels, num_classes, latent_dim, bands, lr, beta1, beta2, adam_eps)

    def build_discriminator(self, in_channels, out_channels, down_samples, num_classes, lr, beta1, beta2, adam_eps):
        return Discriminator(in_channels, out_channels, down_samples, num_classes, lr, beta1, beta2, adam_eps)

    def build_encoder(self, num_ch=31, feature=64):
        return nn.Sequential(
            nn.Conv2d(num_ch, feature, 3, padding=1),
            nn.ReLU(True),
            nn.Conv2d(feature, feature, 3, padding=1),
            nn.ReLU(True),
            nn.Conv2d(feature, feature // 2, 3, padding=1),
            nn.ReLU(True),
            nn.Conv2d(feature // 2, feature // 2, 3, padding=1),
            nn.ReLU(True),
            nn.Conv2d(feature // 2, feature // 4, 3, padding=1),
            nn.ReLU(True),
            nn.Conv2d(feature // 4, feature // 8, 3, padding=1)
        )

    def build_decoder(self, feature=64, num_ch=31):
        return nn.Sequential(
            nn.Conv2d(feature // 8, feature // 4, 3, padding=1),
            nn.ReLU(True),
            nn.Conv2d(feature // 4, feature // 2, 3, padding=1),
            nn.ReLU(True),
            nn.Conv2d(feature // 2, feature // 2, 3, padding=1),
            nn.ReLU(True),
            nn.Conv2d(feature // 2, feature, 3, padding=1),
            nn.ReLU(True),
            nn.Conv2d(feature, feature, 3, padding=1),
            nn.ReLU(True),
            nn.Conv2d(feature, num_ch, 3, padding=1),
            nn.ReLU(True)
        )

    def build_gan(self):
        return GAN(self.generator, self.discriminator, self.encoder, self.decoder)

    def save_images(self, epoch):
        if epoch == 300:
            Path(self.save_path + '/images').mkdir(parents=True, exist_ok=True)

        self.generator.eval()
        self.decoder.eval()

        generated_images = self.generator(self.fixed_z, self.fixed_y)
        generated_images = self.decoder(generated_images)

        generated_images = generated_images.detach().cpu().numpy()
        generated_images = np.transpose(generated_images, (0, 2, 3, 1))

        if generated_images.shape[1] > 3:
            generated_images = self.cs.spec_to_rgb(generated_images)

        plt.figure(figsize=(20, 20))

        for i in range(16):
            plt.subplot(4, 4, i + 1)
            plt.imshow(generated_images[i] * 0.5 + 0.5)
            plt.axis('off')

        plt.savefig('{}/images/generated_{:04d}.png'.format(self.save_path, epoch))
        plt.close()

    # ---------------- Define training loop ---------------- #

    def toggle_grad(self, model, activate):
        for param in model.parameters():
            param.requires_grad = activate

    def train(self, data_loader, epochs):
        for epoch in range(300, epochs + 300):
            accuracy_metrics = {}
            accuracy_iterations = 0

            loop = tqdm(enumerate(data_loader), total=len(data_loader))
            for iter, (x, y) in loop:
                self.generator.train()
                self.discriminator.train()

                x, y = x.to(self.device), y[:, 0].to(self.device, torch.int64)
                metrics = self.train_step(x, y, iter)

                for k, v in metrics.items():
                    if k not in accuracy_metrics:
                        accuracy_metrics[k] = 0
                    accuracy_metrics[k] += v

                accuracy_iterations += 1
                loop.set_postfix(**metrics)

            # ---------------- Log metrics ---------------- #

            if self.writer:
                for k, v in accuracy_metrics.items():
                    self.writer.add_scalar('train_{}'.format(k), v / accuracy_iterations, epoch)

            self.save_images(epoch)

            if int(epoch) % 25 == 0:
                self.save_checkpoint(self.save_path, epoch=epoch)
                print(f'saved checkpoint at epoch {epoch}')

    def train_step(self, inputs, labels, iter):

        # ----------------- Discriminator loss ----------------- #

        self.generator.optim.zero_grad()
        self.discriminator.optim.zero_grad()

        inputs = torch.split(inputs, self.batch_size['gen'])
        labels = torch.split(labels, self.batch_size['gen'])

        counter = 0

        if self.toggle_grads:
            self.toggle_grad(self.generator, activate=False)
            self.toggle_grad(self.discriminator, activate=True)

        for x, y in zip(inputs, labels):
            self.discriminator.optim.zero_grad()

            counter = 0
            discriminator_loss_real_total = 0
            discriminator_loss_fake_total = 0
            discriminator_real_total = 0
            discriminator_fake_total = 0

            self.z.sample_()
            self.y.sample_()

            x.requires_grad = True
            discriminator_scores = self.gan(self.z[:self.batch_size['gen']], self.y[:self.batch_size['gen']], x, y,
                                            cs=None, train_generator=False, policy='')
            discriminator_fake, discriminator_real = discriminator_scores

            # discriminator loss

            discriminator_loss_real, discriminator_loss_fake = loss_hinge_dis(discriminator_fake, discriminator_real,
                                                                              self.ema_losses, iter)

            discriminator_loss = discriminator_loss_real + discriminator_loss_fake
            discriminator_loss.backward()

            # accumulated discriminator losses

            discriminator_loss_real_total += discriminator_loss_real.item()
            discriminator_loss_fake_total += discriminator_loss_fake.item()
            discriminator_real_total += torch.mean(discriminator_real).item()
            discriminator_fake_total += torch.mean(discriminator_fake).item()

            self.discriminator.optim.step()

        # ----------------- Generator loss ----------------- #

        if self.toggle_grads:
            self.toggle_grad(self.generator, activate=True)
            self.toggle_grad(self.discriminator, activate=False)

        self.generator.optim.zero_grad()

        generator_loss_total = 0

        self.z.sample_()
        self.y.sample_()
        discriminator_fake = self.gan(self.z, self.y, cs=None, train_generator=True, policy='')
        generator_loss = loss_hinge_gen(discriminator_fake, discriminator_real_total)
        generator_loss.backward()

        # accumulated generator losses

        generator_loss_total += generator_loss.item()
        self.ema_losses.update(generator_loss_total, 'generator_loss', iter)

        self.generator.optim.step()

        outputs = dict(gen_loss=float(generator_loss_total),
                       dis_loss_real=float(discriminator_loss_real_total),
                       dis_loss_fake=float(discriminator_loss_fake_total),
                       dis_real=float(discriminator_real_total),
                       dis_fake=float(discriminator_fake_total))

        return outputs

    def save_checkpoint(self, path, epoch=None):
        if epoch is None:
            torch.save(self.generator.state_dict(), '{}/generator.pth'.format(path))
            torch.save(self.discriminator.state_dict(), '{}/discriminator.pth'.format(path))
        else:
            torch.save(self.generator.state_dict(), '{}/generator_{}.pth'.format(path, epoch))
            torch.save(self.discriminator.state_dict(), '{}/discriminator_{}.pth'.format(path, epoch))

    def load_checkpoint(self, path, epoch=None):
        if epoch is None:
            self.generator.load_state_dict(torch.load('{}/generator.pth'.format(path)))
            self.discriminator.load_state_dict(torch.load('{}/discriminator.pth'.format(path)))
        else:
            self.generator.load_state_dict(torch.load('{}/generator_{}.pth'.format(path, epoch)))
            self.discriminator.load_state_dict(torch.load('{}/discriminator_{}.pth'.format(path, epoch)))


    def save_ae_checkpoint(self, path, epoch=None):
        if epoch is None:
            torch.save(self.encoder.state_dict(), '{}/encoder.pth'.format(path))
            torch.save(self.decoder.state_dict(), '{}/decoder.pth'.format(path))
        else:
            torch.save(self.encoder.state_dict(), '{}/encoder_{}.pth'.format(path, epoch))
            torch.save(self.decoder.state_dict(), '{}/decoder_{}.pth'.format(path, epoch))

    def load_ae_checkpoint(self, path, epoch=None):
        if epoch is None:
            self.encoder.load_state_dict(torch.load('{}/encoder.pth'.format(path)))
            self.decoder.load_state_dict(torch.load('{}/decoder.pth'.format(path)))
        else:
            self.encoder.load_state_dict(torch.load('{}/encoder_{}.pth'.format(path, epoch)))
            self.decoder.load_state_dict(torch.load('{}/decoder_{}.pth'.format(path, epoch)))


# ---------------- Define functions and models ---------------- #

class Distribution(torch.Tensor):
    def init_distribution(self, dist_type, **kwargs):
        self.dist_type = dist_type
        self.dist_kwargs = kwargs

        if self.dist_type == 'normal':
            self.mean, self.var = kwargs['mean'], kwargs['var']
        elif self.dist_type == 'categorical' or self.dist_type == 'comparison':
            self.num_categories = kwargs['num_categories']
        else:
            raise ValueError('Distribution type not recognized')

    def sample_(self):
        if self.dist_type == 'normal':
            self.normal_(self.mean, self.var)

        elif self.dist_type == 'categorical':
            self.random_(0, self.num_categories)

        elif self.dist_type == 'comparison':
            self.zero_()

    # Silly hack: overwrite the .to() method to wrap the new object
    # in a distribution as well
    def to(self, *args, **kwargs):
        new_obj = Distribution(self)
        try:
            new_obj.init_distribution(self.dist_type, **self.dist_kwargs)
        except:
            pass
        new_obj.data = super().to(*args, **kwargs)
        return new_obj


# ---------------- Generator ---------------- #


class Generator(nn.Module):
    def __init__(self, in_channels, out_channels, num_classes, latent_dim, bands, lr=0.00005, beta1=0.5, beta2=0.999,
                 adam_eps=1e-8):
        super(Generator, self).__init__()
        self.init = 'ortho'

        self.which_conv = partial(SNConv2d, kernel_size=3, padding=1, num_svs=1, num_itrs=1, eps=1e-8)
        self.which_linear = partial(SNLinear, num_svs=1, num_itrs=1, eps=1e-8)
        self.which_bn = partial(CCBN, which_linear=nn.Embedding, input_size=num_classes, norm_style='bn', eps=1e-5)

        self.linear = self.which_linear(latent_dim, 4096)

        self.blocks = nn.ModuleList([GBlock(in_channels=in_channel,
                                            out_channels=out_channel,
                                            which_conv=self.which_conv,
                                            which_bn=self.which_bn,
                                            activation=nn.ReLU(),
                                            upsample=partial(F.interpolate, scale_factor=2)) for
                                     in_channel, out_channel in zip(in_channels, out_channels)])

        self.output = nn.Sequential(BN(output_size=out_channels[-1]), nn.ReLU(),
                                    self.which_conv(out_channels[-1], 8))  # bands))
        self.optim = torch.optim.Adam(self.parameters(), lr=lr, betas=(beta1, beta2), weight_decay=0, eps=adam_eps)

        self.init_weights()

    def init_weights(self):
        self.param_count = 0
        for module in self.modules():
            if isinstance(module, nn.Conv2d) or isinstance(module, nn.Linear) or isinstance(module, nn.Embedding):
                if self.init == 'ortho':
                    nn.init.orthogonal_(module.weight)
                elif self.init == 'N02':
                    nn.init.normal_(module.weight, 0, 0.02)
                elif self.init in ['glorot', 'xavier']:
                    nn.init.xavier_uniform_(module.weight)
                else:
                    print('Init style not recognized...')
                self.param_count += sum([p.data.nelement() for p in module.parameters()])
        print('Param count for G''s initialized parameters: %d' % self.param_count)

    def forward(self, z, y):
        ys = [y] * len(self.blocks)

        h = self.linear(z)
        h = h.view(h.size(0), -1, 4, 4)

        for i, block in enumerate(self.blocks):
            h = block(h, ys[i])

        return torch.tanh(self.output(h))


# ---------------- Generator ---------------- #


class Discriminator(nn.Module):
    def __init__(self, in_channels, out_channels, down_samples, num_classes, lr, beta1, beta2, adam_eps):
        super(Discriminator, self).__init__()
        self.init = 'ortho'

        self.which_conv = partial(SNConv2d, kernel_size=3, padding=1, num_svs=1, num_itrs=1, eps=1e-8)
        self.which_linear = partial(SNLinear, num_svs=1, num_itrs=1, eps=1e-8)
        self.which_embedding = partial(SNEmbedding, num_svs=1, num_itrs=1, eps=1e-8)
        self.activation = nn.ReLU()

        self.blocks = nn.ModuleList([DBlock(in_channels=in_channel,
                                            out_channels=out_channel,
                                            which_conv=self.which_conv,
                                            wide=True,
                                            activation=nn.ReLU(),
                                            preactivation=(i > 0),
                                            downsample=(nn.AvgPool2d(2) if down_sample else None)) for
                                     i, (in_channel, out_channel, down_sample) in
                                     enumerate(zip(in_channels, out_channels, down_samples))])

        self.linear = self.which_linear(out_channels[-1], 1)
        self.embedding = self.which_embedding(num_classes, out_channels[-1])
        self.optim = torch.optim.Adam(self.parameters(), lr=lr, betas=(beta1, beta2), weight_decay=0, eps=adam_eps)

        self.init_weights()

    def init_weights(self):
        self.param_count = 0
        for module in self.modules():
            if isinstance(module, nn.Conv2d) or isinstance(module, nn.Linear) or isinstance(module, nn.Embedding):
                if self.init == 'ortho':
                    nn.init.orthogonal_(module.weight)
                elif self.init == 'N02':
                    nn.init.normal_(module.weight, 0, 0.02)
                elif self.init in ['glorot', 'xavier']:
                    nn.init.xavier_uniform_(module.weight)
                else:
                    print('Init style not recognized...')
                self.param_count += sum([p.data.nelement() for p in module.parameters()])
        print('Param count for D''s initialized parameters: %d' % self.param_count)

    def forward(self, x, y):
        h = x
        for i, block in enumerate(self.blocks):
            h = block(h)

        h = torch.sum(self.activation(h), dim=[2, 3])
        output = self.linear(h)
        output += torch.sum(self.embedding(y) * h, 1, keepdim=True)

        return output


class GAN(nn.Module):
    def __init__(self, generator, discriminator, encoder, decoder):
        super(GAN, self).__init__()
        self.generator = generator
        self.discriminator = discriminator
        self.encoder = encoder
        self.decoder = decoder

    def forward(self, z, gy, x=None, dy=None, cs=None, train_generator=False, only_gz=False, policy=False):
        latent_x = x
        if x is not None:
            with torch.no_grad():
                latent_x = self.encoder(x)

        with torch.set_grad_enabled(train_generator):
            generated = self.generator(z, gy)
            if cs is not None:
                generated = cs.spec_to_rgb_torch(generated)

        discriminator_input = torch.cat([img for img in [generated, latent_x] if img is not None], dim=0)
        discriminator_input = DiffAugment(discriminator_input, policy=policy)
        discriminator_classes = torch.cat([label for label in [gy, dy] if label is not None], dim=0)

        discriminator_target = self.discriminator(discriminator_input, discriminator_classes)

        if x is not None:
            return torch.split(discriminator_target, [generated.size(0), latent_x.size(0)])
        else:
            if only_gz:
                return discriminator_target, generated
            else:
                return discriminator_target
