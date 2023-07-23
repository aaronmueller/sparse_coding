import torch
from abc import ABC, abstractmethod
from torchtyping import TensorType

from autoencoders.ensemble import DictSignature

class LearnedDict(ABC):
    @abstractmethod
    def get_learned_dict(self) -> TensorType["n_dict_components", "activation_size"]:
        pass

    @abstractmethod
    def encode(self, batch: TensorType["batch_size", "activation_size"]) -> TensorType["batch_size", "n_dict_components"]:
        pass
    
    @abstractmethod
    def to_device(self, device):
        pass

    def forward(self, batch: TensorType["batch_size", "activation_size"]) -> TensorType["batch_size", "activation_size"]:
        c = self.encode(batch)
        learned_dict = self.get_learned_dict()
        x_hat = torch.einsum("nd,bn->bd", learned_dict, c)
        return x_hat

class UntiedSAE(LearnedDict):
    def __init__(self, encoder, decoder, encoder_bias):
        self.encoder = encoder
        self.decoder = decoder
        self.encoder_bias = encoder_bias

    def get_learned_dict(self):
        norms = torch.norm(self.decoder, 2, dim=-1)
        return self.decoder / torch.clamp(norms, 1e-8)[:, None]
    
    def to_device(self, device):
        self.encoder = self.encoder.to(device)
        self.decoder = self.decoder.to(device)
        self.encoder_bias = self.encoder_bias.to(device)

    def encode(self, batch):
        c = torch.einsum("nd,bd->bn", self.encoder, batch)
        c = c + self.encoder_bias
        c = torch.clamp(c, min=0.0)
        return c

class TiedSAE(LearnedDict):
    def __init__(self, encoder, encoder_bias, norm_encoder=False):
        self.encoder = encoder
        self.encoder_bias = encoder_bias
        self.norm_encoder = norm_encoder

    def get_learned_dict(self):
        norms = torch.norm(self.encoder, 2, dim=-1)
        return self.encoder / torch.clamp(norms, 1e-8)[:, None]

    def to_device(self, device):
        self.encoder = self.encoder.to(device)
        self.encoder_bias = self.encoder_bias.to(device)
    
    def encode(self, batch):
        if self.norm_encoder:
            norms = torch.norm(self.encoder, 2, dim=-1)
            encoder = self.encoder / torch.clamp(norms, 1e-8)[:, None]
        else:
            encoder = self.encoder

        c = torch.einsum("nd,bd->bn", encoder, batch)
        c = c + self.encoder_bias
        c = torch.clamp(c, min=0.0)
        return c