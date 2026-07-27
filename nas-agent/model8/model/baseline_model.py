import torch
import torch.nn as nn


class SignalAttention1D(nn.Module):
    def __init__(self, embed_dim, num_symbols, num_subcarriers, b_flg=True, m_type="t1"):
        super().__init__()
        self.embed_dim = embed_dim
        self.num_symbols = num_symbols
        self.num_subcarriers = num_subcarriers
        self.m_type = m_type

        self.s = num_subcarriers ** -0.5 if m_type == "t1" else embed_dim ** -0.5

        self.ln = nn.LayerNorm([embed_dim, num_symbols, num_subcarriers], elementwise_affine=False)
        self.sm = nn.Softmax(dim=-1)

        self.p_lyr = nn.Conv1d(
            in_channels=embed_dim,
            out_channels=3 * embed_dim,
            kernel_size=3,
            padding=1,
            bias=b_flg
        )

    def forward(self, x):
        batch, num_syms, embed_dim, num_subs = x.shape

        x = x.permute(0, 2, 1, 3)
        x = self.ln(x)
        x = x.permute(0, 2, 1, 3)

        x_f = torch.reshape(x, [batch * num_syms, embed_dim, num_subs])
        qkv = self.p_lyr(x_f)
        qkv = torch.reshape(qkv, [batch, num_syms, 3 * self.embed_dim, num_subs])

        q = qkv[:, :, 0:self.embed_dim, :]
        k = qkv[:, :, self.embed_dim:2 * self.embed_dim, :]
        v = qkv[:, :, 2 * self.embed_dim:, :]

        if self.m_type == "t1":
            q = q.permute(0, 2, 1, 3)
            k = k.permute(0, 2, 1, 3)
            v = v.permute(0, 2, 1, 3)

            dots = torch.matmul(q, k.transpose(-1, -2)) * self.s
            at = self.sm(dots)
            out = torch.matmul(at, v).permute(0, 2, 1, 3)
        else:
            q = q.permute(0, 3, 1, 2)
            k = k.permute(0, 3, 1, 2)
            v = v.permute(0, 3, 1, 2)

            dots = torch.matmul(q, k.transpose(-1, -2)) * self.s
            at = self.sm(dots)
            out = torch.matmul(at, v).permute(0, 2, 3, 1)

        return out


class SignalFeedForward1D(nn.Module):
    def __init__(self, embed_dim, num_symbols, num_subcarriers, b_flg=True):
        super().__init__()
        self.embed_dim = embed_dim
        self.ln = nn.LayerNorm([num_symbols, embed_dim, num_subcarriers], elementwise_affine=False)
        self.cv1 = nn.Conv1d(in_channels=embed_dim, out_channels=2 * embed_dim, kernel_size=3, padding=1, bias=b_flg)
        self.act = nn.GELU()
        self.cv2 = nn.Conv1d(in_channels=2 * embed_dim, out_channels=embed_dim, kernel_size=3, padding=1, bias=b_flg)

    def forward(self, x):
        batch, num_syms, embed_dim, num_subs = x.shape
        x = self.ln(x)
        x_f = torch.reshape(x, [batch * num_syms, embed_dim, num_subs])
        x = self.cv1(x_f)
        x = self.act(x)
        x = self.cv2(x)
        return torch.reshape(x, [batch, num_syms, embed_dim, num_subs])


class SignalTransformerBlock(nn.Module):
    def __init__(self, embed_dim, num_symbols, num_subcarriers, m_type="t1"):
        super().__init__()
        self.m_a = SignalAttention1D(embed_dim, num_symbols, num_subcarriers, m_type=m_type)

        self.proj = nn.Conv1d(in_channels=embed_dim, out_channels=embed_dim, kernel_size=3, padding=1, bias=False)

        self.m_c = SignalFeedForward1D(embed_dim, num_symbols, num_subcarriers)

    def forward(self, x):
        batch, num_syms, embed_dim, num_subs = x.shape
        x_a = self.m_a(x)

        x_f_f = torch.reshape(x_a, [batch * num_syms, -1, num_subs])
        x_p = self.proj(x_f_f)
        x_p = torch.reshape(x_p, [batch, num_syms, embed_dim, num_subs])
        x = x_p + x

        x_m_c = self.m_c(x)
        x = x_m_c + x
        return x


class SignalProcessingTransformer(nn.Module):
    def __init__(self, in_channels=4, embed_dim=16, num_symbols=64, num_subcarriers=48, bias_flag=True):
        super().__init__()
        self.in_channels = in_channels
        self.embed_dim = embed_dim
        self.num_symbols = num_symbols
        self.num_subcarriers = num_subcarriers
        self.b_flg = bias_flag

        self.e_lyr = nn.Conv1d(in_channels=self.in_channels, out_channels=self.embed_dim, kernel_size=3, padding=1, bias=self.b_flg)

        self.main = nn.Sequential(
            SignalTransformerBlock(self.embed_dim, self.num_symbols, self.num_subcarriers, m_type="t1"),
            SignalTransformerBlock(self.embed_dim, self.num_symbols, self.num_subcarriers, m_type="t1"),
            SignalTransformerBlock(self.embed_dim, self.num_symbols, self.num_subcarriers, m_type="t1"),
            SignalTransformerBlock(self.embed_dim, self.num_symbols, self.num_subcarriers, m_type="t1")
        )

        self.r_out = nn.Conv1d(in_channels=self.embed_dim, out_channels=self.in_channels, kernel_size=3, padding=1, bias=self.b_flg)

    def forward(self, inp: torch.Tensor):
        if inp.dim() == 5 and inp.shape[-1] == 1:
            inp = torch.squeeze(inp, dim=-1)

        B, num_ports, num_subcarriers, num_symbols = inp.shape

        alpha = torch.sqrt(torch.mean(inp ** 2, dim=[1, 2, 3], keepdim=True) * 2)
        x = inp / (alpha + 1e-6)

        x = x.permute(0, 3, 1, 2)
        x = torch.reshape(x, [B * num_symbols, num_ports, num_subcarriers])
        x = self.e_lyr(x)

        x = torch.reshape(x, [B, num_symbols, -1, num_subcarriers])

        x = self.main(x)

        x = torch.reshape(x, [B * num_symbols, -1, num_subcarriers])
        x = self.r_out(x)
        x = torch.reshape(x, [B, num_symbols, num_ports, num_subcarriers])
        x = x.permute(0, 2, 3, 1)

        x = x * alpha

        x = torch.unsqueeze(x, dim=-1)

        return x


if __name__ == "__main__":
    # 运行示例
    model = SignalProcessingTransformer()
    model.eval()  # 评估模式
    B, num_ports, num_subcarriers, num_symbols = 1, 4, 48, 64
    dummy_input = torch.randn(B, num_ports, num_subcarriers, num_symbols, 1)
    with torch.no_grad():
        try:
            output = model(dummy_input)
            print("Inference successful!")
            print(f"Output Tensor Shape: {output.shape}")

            assert output.shape == dummy_input.shape
            print("Verification: Output shape matches input shape.")

        except Exception as e:
            print(f"Error during inference: {e}")