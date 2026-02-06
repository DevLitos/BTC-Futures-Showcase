"""
Path Setup Utility - SINGLE SOURCE for project path configuration

╔══════════════════════════════════════════════════════════════════════════════╗
║ 📦 CENTRALIZED PATH SETUP                                                    ║
╠══════════════════════════════════════════════════════════════════════════════╣
║ This module provides a single function to ensure the project root is in     ║
║ sys.path. All entry points (cmd/, tools/, tests/) should use this.          ║
║                                                                              ║
║ Usage:                                                                       ║
║   from src.bootstrap.path_setup import ensure_project_root                   ║
║   ensure_project_root()                                                      ║
╚══════════════════════════════════════════════════════════════════════════════╝

Created: Feb 05, 2026
Reason: Path setup was duplicated in 10+ files
"""

import sys
from pathlib import Path
from typing import Optional


def ensure_project_root(caller_file: Optional[str] = None) -> Path:
    """
    Ensure the project root directory is in sys.path.
    
    This function finds the project root (directory containing src/) and adds
    it to sys.path if not already present. This allows imports like
    `from src.xxx import yyy` to work correctly.
    
    Args:
        caller_file: Optional __file__ of the calling module. If provided,
                     the project root is calculated relative to this file.
                     If None, uses the location of this module.
    
    Returns:
        Path to the project root directory
        
    Example:
        # In cmd/live.py:
        from src.bootstrap.path_setup import ensure_project_root
        ensure_project_root(__file__)
        
        # Or simpler (works if src/ is already accessible):
        from src.bootstrap.path_setup import ensure_project_root
        ensure_project_root()
    """
    if caller_file:
        # Calculate root from caller's location
        caller_path = Path(caller_file).resolve()
        # Traverse up until we find a directory containing 'src/'
        root = caller_path.parent
        while root.parent != root:
            if (root / 'src').is_dir():
                break
            root = root.parent
    else:
        # This file is at src/bootstrap/path_setup.py
        # Project root is ../../ from here
        root = Path(__file__).resolve().parent.parent.parent
    
    root_str = str(root)
    
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    
    return root


def get_project_root() -> Path:
    """
    Get the project root directory.
    
    Returns:
        Path to the project root (directory containing src/)
    """
    return Path(__file__).resolve().parent.parent.parent


# For convenience, ensure path is set on import
# This allows simply doing: import src.bootstrap.path_setup
# and the path will be configured automatically
_PROJECT_ROOT = ensure_project_root()
