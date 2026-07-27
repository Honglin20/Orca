import torch.nn as nn


class ChoiceLayer(nn.Module):
    def __init__(self, *, branches: dict[str, nn.Module]):
        super().__init__()
        self.branches = nn.ModuleDict(branches)

        if len(self.branches) == 0:
            raise ValueError("ChoiceContainer must contain at least one branch.")
        self.choice_name = next(iter(self.branches))

    def set_sample_config(self, *, choice_name: str, **choice_kwargs):
        self.choice_name = choice_name
        self.branches[choice_name].set_sample_config(**choice_kwargs)

    def forward(self, *args, **kwargs):
        return self.branches[self.choice_name](*args, **kwargs)

    def get_active_subnet(self):
        return self.branches[self.choice_name].get_active_subnet()

    @property
    def elastic_num_params(self):
        return self.branches[self.choice_name].elastic_num_params
