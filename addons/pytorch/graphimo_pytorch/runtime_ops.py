"""Small addressable PyTorch modules for functional visual operations."""

from __future__ import annotations

import torch
from torch import nn
from torch.nn import functional as F


class ReshapeOp(nn.Module):
    def __init__(self, target_shape: tuple[int | str, ...], input_symbols: tuple[str, ...]):
        super().__init__()
        self.target_shape = target_shape
        self.input_symbols = input_symbols

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        symbols = {symbol: x.shape[index] for index, symbol in enumerate(self.input_symbols) if symbol}
        shape = tuple(_resolve_dimension(token, symbols) for token in self.target_shape)
        return x.reshape(shape)


class TransposeOp(nn.Module):
    def __init__(self, dim0: int, dim1: int):
        super().__init__()
        self.dim0, self.dim1 = dim0, dim1

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return x.transpose(self.dim0, self.dim1)


class SplitHeadsOp(nn.Module):
    def __init__(self, n_heads: int):
        super().__init__()
        self.n_heads = n_heads

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        batch, sequence, width = x.shape
        return x.reshape(batch, sequence, self.n_heads, width // self.n_heads).transpose(1, 2)


class MergeHeadsOp(nn.Module):
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return x.transpose(1, 2).flatten(2)


class AddOp(nn.Module):
    def forward(self, left: torch.Tensor, right: torch.Tensor) -> torch.Tensor:
        return left + right


class MultiplyOp(nn.Module):
    def forward(self, left: torch.Tensor, right: torch.Tensor) -> torch.Tensor:
        return left * right


class ScaleOp(nn.Module):
    def __init__(self, factor: float):
        super().__init__()
        self.factor = factor

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return x * self.factor


class MatMulOp(nn.Module):
    def forward(self, left: torch.Tensor, right: torch.Tensor) -> torch.Tensor:
        return torch.matmul(left, right)


class RoPEOp(nn.Module):
    def __init__(self, base: float = 10000.0):
        super().__init__()
        self.base = base

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        sequence, width = x.shape[-2:]
        inv_freq = 1.0 / self.base ** (
            torch.arange(0, width, 2, device=x.device, dtype=torch.float32) / width
        )
        angles = torch.outer(torch.arange(sequence, device=x.device, dtype=torch.float32), inv_freq)
        angles = torch.cat((angles, angles), dim=-1)
        left, right = x[..., : width // 2], x[..., width // 2 :]
        rotated = torch.cat((-right, left), dim=-1)
        return x * angles.cos().to(x.dtype) + rotated * angles.sin().to(x.dtype)


class CausalMaskOp(nn.Module):
    def forward(self, scores: torch.Tensor) -> torch.Tensor:
        sequence = scores.shape[-1]
        mask = torch.triu(torch.ones((sequence, sequence), dtype=torch.bool, device=scores.device), diagonal=1)
        return scores.masked_fill(mask, float("-inf"))


class ConcatenateOp(nn.Module):
    def __init__(self, dim: int):
        super().__init__()
        self.dim = dim

    def forward(self, *tensors: torch.Tensor) -> torch.Tensor:
        return torch.cat(tensors, dim=self.dim)


class UnflattenOp(nn.Module):
    def __init__(self, dim: int, sizes: tuple[int | str, ...], input_symbols: tuple[str, ...]):
        super().__init__()
        self.dim = dim
        self.sizes = sizes
        self.input_symbols = input_symbols

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        symbols = {symbol: x.shape[index] for index, symbol in enumerate(self.input_symbols) if symbol}
        sizes = tuple(_resolve_dimension(token, symbols) for token in self.sizes)
        return x.unflatten(self.dim, sizes)


class CrossEntropyOp(nn.Module):
    def __init__(self, mode: str):
        super().__init__()
        self.mode = mode

    def forward(self, logits: torch.Tensor, targets: torch.Tensor) -> torch.Tensor:
        if self.mode == "next_token":
            return F.cross_entropy(logits[:, :-1].flatten(0, -2), targets[:, 1:].flatten())
        return F.cross_entropy(logits, targets)


def _resolve_dimension(token: int | str, symbols: dict[str, int]) -> int:
    if type(token) is int:
        return token
    if token == "-1":
        return -1
    if token in symbols:
        return symbols[token]
    for operator in ("/", "*"):
        if operator in token:
            left, right = token.split(operator, 1)
            lhs = _resolve_dimension(left, symbols)
            rhs = _resolve_dimension(right, symbols)
            if operator == "/":
                return lhs // rhs
            return lhs * rhs
    raise ValueError(f"Cannot resolve symbolic dimension {token!r} from the input tensor")

