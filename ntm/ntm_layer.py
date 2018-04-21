import torch
from torch import nn
from data_gen.plot_data import plot_memory_attention
import numpy as np

from ntm.ntm_cell import NTMCell


class NTM(nn.Module):

    def __init__(self, tm_in_dim, tm_output_units, tm_state_units,
                 num_heads, is_cam, num_shift, M):
        """Initialize an NTM Layer.

        :param tm_in_dim: input size.
        :param tm_output_units: output size.
        :param tm_state_units: state size.
        :param num_heads: number of heads.
        :param is_cam: is it content_addressable.
        :param num_shift: number of shifts of heads.
        :param M: Number of slots per address in the memory bank.
        """
        super(NTM, self).__init__()

        # Create the NTM components
        self.NTMCell = NTMCell(tm_in_dim, tm_output_units, tm_state_units,
                               num_heads, is_cam, num_shift, M)

    def forward(self, x, state):           # x : batch_size, seq_len, input_size
        output = None
        for j in range(x.size()[-2]):
            tm_output, state = self.NTMCell(x[..., j, :], state)

            # plot attention/memory
            plot_active = False
            if plot_active:
                label = 'Write/Read sequences x,y'
                plot_memory_attention(state[2], state[1], label)

            if tm_output is None:
                continue

            tm_output = tm_output[..., None, :]
            if output is None:
                output = tm_output
                continue

            # concatenate output
            output = torch.cat([output, tm_output], dim=-2)

        return output, state


