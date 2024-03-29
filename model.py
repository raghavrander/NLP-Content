# model from the paper `Content Preserving Text Generation with Attribute Controls`

import torch
import torch.nn as nn
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence

from utils import make_one_hot, truncate, append, sequence_mask, get_actual_lengths, sort_by_length
from dataloading import SOS_IDX, EOS_IDX, PAD_IDX


# TODO: generation max length from shen et al.
MAXLEN = 30


# TODO: dropout?
class CPTG(nn.Module):
    def __init__(self, generator, discriminator):
        super().__init__()
        self.generator = generator
        self.discriminator = discriminator

    def forward(self, x, l=None, l_=None, is_gen=False):
        """
        x: tuple of (B, L+1) and (B,)
        l: (B, )
        l_: (B, )
        """
        # TODO: come up with a more readable code for generation
        #try:
        #    hx, hy, gen_output = self.generator(x, l, l_, is_gen)
        #except ValueError: # when generating
        #    y = self.generator(x, l, l_, is_gen)
        #    return y
        if is_gen:
            y = self.generator(x, l, l_, is_gen)
            return y
        else:
            hx, hy, gen_output = self.generator(x, l, l_, is_gen)
        dis_output = self.discriminator(hx, hy, l, l_)
        return gen_output, dis_output # tuple (B, MAXLEN, 700), (B,) and
                                      # tuple of hx_l, hy_l_, hx_l_
                                      # hx_l: (B,)
class Generator(nn.Module):
    def __init__(self, encoder, decoder, gamma):
        super().__init__()
        self.encoder = encoder
        self.decoder = decoder
        self.gamma = gamma

    def _fuse(self, z_x, z_y):
        g = torch.empty_like(z_x).bernoulli_(self.gamma) # (B, 500)
        z_xy = (g * z_x) + ((1 - g) * z_y)
        return z_xy # (B, 500)

    def forward(self, x, l, l_, is_gen):
        """
        x: tuple of (B, L+1) and (B,)
        l: (B,)
        l_: (B,)
        """
        z_x = self.encoder(x)
        hy, y = self.decoder(z_x, l_)
        if is_gen:
            return y
        z_y = self.encoder(y)
        z_xy = self._fuse(z_x, z_y)
        hx, output = self.decoder(z_xy, l, x)
        return hx, hy, output


class Encoder(nn.Module):
    def __init__(self, vocab):
        super().__init__()
        # TODO: use pretrained GLOVE
        self.word_emb = nn.Embedding(vocab, 300)
        self.gru = nn.GRU(300, 500, batch_first=True)

    def forward(self, x):
        """
        x: tuple of (B,L+1) and (B,)
        """
        x, lengths = x
        x_embed = self.word_emb(x) #(B, L+1, 300)
        packed_in = pack_padded_sequence(x_embed, lengths, batch_first=True)
        _, z_x = self.gru(packed_in)
        return z_x.squeeze() # (B, 500)


class Decoder(nn.Module):
    def __init__(self, vocab, attr):
        super().__init__()
        self.emb = nn.Embedding(vocab, 300) # FIXME: sharing?
        self.attr_emb = nn.Embedding(attr, 200)
        self.gru = nn.GRU(300, 700, batch_first=True)
        self.out = nn.Linear(700, vocab)

    # this does not backpropagate at all
    def _hard_sampling(self, output):
        # output (B, 1, vocab)
        prob = output.squeeze(1).softmax(dim=-1)
        sampled = torch.multinomial(prob, num_samples=1)
        #len_ = (sampled != EOS_IDX).squeeze(1).long()
        return sampled.detach()#, len_ # (B, 1), (B,)

    #def _tighten(self, hy, y):
    #    """
    #    pad tokens after EOS and mask hiddens after EOS
    #    hy: (B, MAXLEN+1, 700
    #    y: (B, MAXLEN+1)
    #    """
    #    # DEBUG: inconsistency in results with argmax
    #    lengths = (y == EOS_IDX).argmax(dim=1) + 1 # (B,) +1 for 0 index
    #    mask = sequence_mask(lengths)
    #    y.masked_fill_((mask!=1), PAD_IDX) # this does not backprop
    #    hy = hy * (mask.unsqueeze(-1)).float()
    #    hy, y, lengths = sort_by_length(hy, y, lengths)
    #    return hy, y, lengths

    def _tighten(self, hy, y):
        """
        pad tokens after EOS and mask hiddens after EOS
        hy: (B, MAXLEN+1, 700
        y: (B, MAXLEN+1)
        """
        lengths = get_actual_lengths(y)
        mask = sequence_mask(lengths)
        y = y[:, :mask.size(1)] # truncate unnecessarily generated part
        hy = hy[:, :mask.size(1)]
        y.masked_fill_((mask!=1), PAD_IDX) # this does not backprop
        hy = hy * (mask.unsqueeze(-1)).float()
        hy, y, lengths = sort_by_length(hy, y, lengths)
        return hy, y, lengths

    def forward(self, z, l, x=None):
        """
        z: (B, 500)
        l: (B,)
        x: tuple of (B, L+1), (B,)
        """
        B = l.size(0)
        l_embed = self.attr_emb(l) # (B, 200)
        hidden = torch.cat([z, l_embed], dim=-1).unsqueeze(0) # (1, B, 700)

        if x is not None: # loss computation with teacher forcing
            x, lengths = append(truncate(x, 'eos'), 'sos')
            x_embed = self.emb(x) # (B, L+1, 300)
            packed_in = pack_padded_sequence(x_embed, lengths, batch_first=True)
            packed_out, _ = self.gru(packed_in, hidden)
            total_length = x.size(1)
            # (B, L, 700)
            hx, lengths = pad_packed_sequence(packed_out, batch_first=True,
                                                  total_length=total_length)
            output = self.out(hx)
            return (hx, lengths), (output, lengths) # (B, L+1, 700), (B,)
                                                    # (B, L+1, vocab), (B,)
        else: # sample y
            y = []
            hy = []
            input_ = l.new_full((B, 1), SOS_IDX)
            for t in range(MAXLEN):
                input_ = self.emb(input_) # (B, 1, 300)
                # output (B, 1, 700), hidden (1, B, 700)
                output, hidden = self.gru(input_, hidden)
                input_ = self._hard_sampling(self.out(output))
                hy.append(output)
                y.append(input_)
            input_ = l.new_full((B,1), EOS_IDX) # feed <eos> as last input,
            output, _ = self.gru(self.emb(input_), hidden)
            hy.append(output)
            y.append(input_) # append <eos> as last token

            hy = torch.cat(hy, dim=1)
            y = torch.cat(y, dim=1)
            hy, y, lengths = self._tighten(hy, y)
            #lengths = y.new_full((B,), MAXLEN+1)
            return (hy, lengths), (y, lengths) # (B, MAXLEN+1, 700), (B, MAXLEN+1), (B, )


class Discriminator(nn.Module):
    def __init__(self, attr):
        super().__init__()
        self.attr = attr
        # FIXME: GRU?
        self.birnn = nn.GRU(700, 500, batch_first=True, bidirectional=True)
        self.W = nn.Linear(500 * 2, attr)
        self.v = nn.Parameter(torch.randn(500 * 2))

    def _phi(self, h):
        """
        h: tuple of (B, L+1, 700) and (B,)
        """
        h, lengths = h
        B, total_length, _ = h.size()
        packed_in = pack_padded_sequence(h, lengths, batch_first=True)
        packed_out, hidden = self.birnn(packed_in)
        last_hidden = torch.cat([hidden[0], hidden[1]], dim=-1)
        return last_hidden # (B, 500*2)

    def _discriminator(self, h, l):
        last_hidden = self._phi(h)
        l_onehot = make_one_hot(l, self.attr)
        term1 = torch.sum(l_onehot * self.W(last_hidden), dim=1)
        term2 = torch.sum(self.v * last_hidden, dim=1)
        return term1 + term2 # (B,), logit not prob

    def forward(self, hx, hy, l, l_):
        """
        hx: tuple of (B, L, 700), (B,)
        hy: tuple of (B, MAXLEN, 700), (B,)
        l: (B, )
        """
        hx_l = self._discriminator(hx, l)
        # FIXME: detach needed?!?!
        #h, length = hy
        #hy = h.detach(), length
        hy_l_ = self._discriminator(hy, l_)
        hx_l_ = self._discriminator(hx, l_)
        return hx_l, hy_l_, hx_l_


def make_model(vocab, attr, gamma=0.5, device=torch.device('cuda')):
    encoder = Encoder(vocab)
    decoder = Decoder(vocab, attr)
    generator = Generator(encoder, decoder, gamma)
    discriminator = Discriminator(attr)
    return CPTG(generator, discriminator).to(device)

