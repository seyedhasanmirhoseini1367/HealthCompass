"""
One-time script: convert the three .pth seizure models to .onnx with dynamic time_steps.
Run with the local venv: .venv\Scripts\python convert_to_onnx.py
"""
import sys
import os
os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))

import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from pathlib import Path

_MODELS_DIR = Path(__file__).parent / 'ai_insights' / 'models'


# ── Architecture builders ─────────────────────────────────────────────────────

def _build_cnn_simple():
    class CNNSimple(nn.Module):
        def __init__(self):
            super().__init__()
            self.conv1 = nn.Sequential(
                nn.Conv2d(1, 64, kernel_size=7, padding=3),
                nn.BatchNorm2d(64), nn.GELU(), nn.MaxPool2d(2, 2),
            )
            self.conv2 = nn.Sequential(
                nn.Conv2d(64, 128, kernel_size=5, padding=2),
                nn.BatchNorm2d(128), nn.GELU(), nn.MaxPool2d(2, 2),
            )
            self.global_pool    = nn.AdaptiveAvgPool2d((1, 1))
            self.self_attention = nn.MultiheadAttention(embed_dim=128, num_heads=4, batch_first=True)
            self.layer_norm     = nn.LayerNorm(128)
            self.fc1            = nn.Linear(128, 64)
            self.fc2            = nn.Linear(64, 2)
            self.dropout        = nn.Dropout(0.3)

        def forward(self, x):
            batch, n_ch, freq, time = x.shape
            ch_features = []
            for c in range(n_ch):
                ch = x[:, c:c+1, :, :]
                ch = self.conv1(ch)
                ch = self.conv2(ch)
                ch = self.global_pool(ch)
                ch = ch.squeeze(-1).squeeze(-1)
                ch_features.append(ch)
            seq = torch.stack(ch_features, dim=1)
            attn_out, _ = self.self_attention(seq, seq, seq)
            out = self.layer_norm(attn_out.mean(dim=1))
            out = self.dropout(torch.relu(self.fc1(out)))
            return self.fc2(out)

    return CNNSimple()


def _build_gru_attention(state_dict: dict):
    ih         = state_dict['gru.weight_ih_l0']
    gru_in     = int(ih.shape[1])
    gru_hidden = int(ih.shape[0]) // 3
    bidir      = 'gru.weight_ih_l0_reverse' in state_dict
    gru_out    = gru_hidden * (2 if bidir else 1)

    has_in_norm   = 'input_norm_gru.weight' in state_dict
    in_norm_is_bn = has_in_norm and 'input_norm_gru.running_mean' in state_dict
    in_norm_size  = int(state_dict['input_norm_gru.weight'].shape[0]) if has_in_norm else None

    has_gru_norm   = 'gru_norm.weight' in state_dict
    gru_norm_is_bn = has_gru_norm and 'gru_norm.running_mean' in state_dict
    gru_norm_size  = int(state_dict['gru_norm.weight'].shape[0]) if has_gru_norm else None

    has_proj = 'projection.weight' in state_dict
    proj_out = int(state_dict['projection.weight'].shape[0]) if has_proj else gru_out

    embed_dim = int(state_dict['self_attention.in_proj_weight'].shape[1])
    num_heads = 4 if embed_dim % 4 == 0 else (2 if embed_dim % 2 == 0 else 1)

    cl_in     = int(state_dict['classification_head.0.weight'].shape[1])
    cl_hidden = int(state_dict['classification_head.0.weight'].shape[0])
    use_skip  = (cl_in == 2 * embed_dim)

    class GRUAttentionModel(nn.Module):
        def __init__(self):
            super().__init__()
            if has_in_norm:
                self.input_norm_gru = (nn.BatchNorm1d(in_norm_size) if in_norm_is_bn
                                       else nn.LayerNorm(in_norm_size))
            self.gru = nn.GRU(input_size=gru_in, hidden_size=gru_hidden,
                              batch_first=True, bidirectional=bidir)
            if has_gru_norm:
                self.gru_norm = (nn.BatchNorm1d(gru_norm_size) if gru_norm_is_bn
                                 else nn.LayerNorm(gru_norm_size))
            if has_proj:
                self.projection = nn.Linear(gru_out, proj_out)
            self.self_attention = nn.MultiheadAttention(
                embed_dim=embed_dim, num_heads=num_heads, batch_first=True)
            self.classification_head = nn.Sequential(
                nn.Linear(cl_in, cl_hidden),
                nn.LayerNorm(cl_hidden),
                nn.GELU(),
                nn.Dropout(0.5),
                nn.Linear(cl_hidden, 2),
            )

        def forward(self, x):
            batch_size, seq_len, num_channels, input_size = x.shape
            x = x.permute(0, 2, 1, 3).contiguous()
            x = x.reshape(batch_size * num_channels, seq_len, input_size)
            if has_in_norm:
                if in_norm_is_bn:
                    B_C, S, F = x.shape
                    x = self.input_norm_gru(x.reshape(B_C * S, F)).reshape(B_C, S, F)
                else:
                    x = self.input_norm_gru(x)
            gru_out, _ = self.gru(x)
            last = gru_out[:, -1, :]
            if has_gru_norm:
                last = self.gru_norm(last)
            last = last.reshape(batch_size, num_channels, -1)
            if has_proj:
                last = self.projection(last)
            attn_out, _ = self.self_attention(last, last, last)
            if use_skip:
                pooled = torch.cat([last.mean(dim=1), attn_out.mean(dim=1)], dim=-1)
            else:
                pooled = attn_out.mean(dim=1)
            return self.classification_head(pooled)

    return GRUAttentionModel()


def _build_fusion():
    class FusionClassifier(nn.Module):
        def __init__(self):
            super().__init__()
            self.conv_layers = nn.Sequential(
                nn.Conv2d(1, 64, kernel_size=7, padding=3),
                nn.BatchNorm2d(64), nn.GELU(), nn.MaxPool2d(2),
                nn.Conv2d(64, 96, kernel_size=5, padding=2),
                nn.BatchNorm2d(96), nn.GELU(), nn.MaxPool2d(2),
            )
            self.adaptive_pool  = nn.AdaptiveAvgPool2d((1, 1))
            self.gru            = nn.GRU(input_size=200, hidden_size=32, batch_first=True)
            self.embedding      = nn.Linear(128, 128)
            self.self_attention = nn.MultiheadAttention(embed_dim=128, num_heads=4, batch_first=True)
            self.layer_norm     = nn.LayerNorm(128)
            self.dropout        = nn.Dropout(0.5)
            self.classifier     = nn.Sequential(
                nn.Linear(128, 64), nn.GELU(), nn.Dropout(0.5), nn.Linear(64, 2),
            )

        def forward(self, cnnx, lstmx):
            batch_size, height, num_channels, width = cnnx.shape
            cnnx = cnnx.permute(0, 2, 1, 3).contiguous()
            cnnx = cnnx.reshape(batch_size * num_channels, 1, height, width)
            cnnx = self.conv_layers(cnnx)
            cnnx = self.adaptive_pool(cnnx)
            cnn_out = cnnx.reshape(batch_size, num_channels, -1)

            batch_size_l, seq_len, num_channels_l, input_size = lstmx.shape
            lstmx = lstmx.permute(0, 2, 1, 3).contiguous()
            lstmx = lstmx.reshape(batch_size_l * num_channels_l, seq_len, input_size)
            gru_out, _ = self.gru(lstmx)
            gru_out = gru_out[:, -1, :].reshape(batch_size, num_channels, -1)

            fused = torch.cat((cnn_out, gru_out), dim=-1)
            fused = self.embedding(fused)
            attn_out, _ = self.self_attention(fused, fused, fused)
            attn_out = self.layer_norm(attn_out.mean(dim=1))
            return self.classifier(self.dropout(attn_out))

    return FusionClassifier()


# ── Export helpers ─────────────────────────────────────────────────────────────

def _export(model, args, out_path, input_names, output_names, dynamic_axes=None):
    torch.onnx.export(
        model, args, str(out_path),
        input_names=input_names,
        output_names=output_names,
        dynamic_axes=dynamic_axes,
        opset_version=17,
        dynamo=False,
    )
    print(f'Saved: {out_path}  ({out_path.stat().st_size // 1024} KB)')


if __name__ == '__main__':
    print('Exporting cnn_transformer...')
    sd = torch.load(str(_MODELS_DIR / 'cnn_transformer.pth'), map_location='cpu', weights_only=True)
    m  = _build_cnn_simple()
    m.load_state_dict(sd)
    m.eval()
    _export(m, torch.zeros(1, 19, 65, 33), _MODELS_DIR / 'cnn_transformer.onnx',
            ['x'], ['logits'],
            dynamic_axes={'x': {0: 'batch', 3: 'time_steps'}, 'logits': {0: 'batch'}})

    print('Exporting gru_attention...')
    sd = torch.load(str(_MODELS_DIR / 'gru_attention.pth'), map_location='cpu', weights_only=True)
    m  = _build_gru_attention(sd)
    m.load_state_dict(sd)
    m.eval()
    _export(m, torch.zeros(1, 10, 19, 200), _MODELS_DIR / 'gru_attention.onnx',
            ['x'], ['logits'],
            dynamic_axes={'x': {0: 'batch'}, 'logits': {0: 'batch'}})

    print('Exporting fusion...')
    sd = torch.load(str(_MODELS_DIR / 'fusion.pth'), map_location='cpu', weights_only=True)
    m  = _build_fusion()
    m.load_state_dict(sd)
    m.eval()
    _export(m, (torch.zeros(1, 65, 19, 33), torch.zeros(1, 10, 19, 200)),
            _MODELS_DIR / 'fusion.onnx',
            ['cnnx', 'lstmx'], ['logits'],
            dynamic_axes={
                'cnnx':   {0: 'batch', 3: 'time_steps'},
                'lstmx':  {0: 'batch'},
                'logits': {0: 'batch'},
            })

    print('Done. All 3 models exported to ONNX.')
