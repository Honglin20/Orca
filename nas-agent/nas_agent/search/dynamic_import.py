"""Helpers for loading generated search modules from config."""

import importlib

import sys
from typing import Any


def load_generated_component(component_path: str) -> Any:
    """Load an importable reference.

    Loads a component such as module.path.ClassName or module.path.submodule.

    Args:
        component_path (str): The import path of the component to load.

    Returns:
        Any: The loaded component, which can be a class, function, or module.

    Raises:
        ValueError: If the provided component_path is empty.
    """
    if not component_path:
        raise ValueError("Provided component path is empty.")

    if "" not in sys.path and "." not in sys.path:
        sys.path.insert(0, ".")

    component_path = str(component_path)
    if "." not in component_path:
        return importlib.import_module(component_path)
    module_path, attr_name = component_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, attr_name)
