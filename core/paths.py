import os
from pathlib import Path
from typing import Union, Set, List, Tuple

def resolve_under_root(
    raw: str, 
    root: Union[str, Path], 
    *, 
    must_exist: bool = False,
    must_be_dir: bool = False,
    must_be_file: bool = False,
    exts: Union[Set[str], List[str], Tuple[str, ...], None] = None,
    deny_root: bool = False,
    max_len: int = 1024
) -> Union[str, None]:
    """Resolve and validate a path to ensure it safely resides under a given root.
    
    Returns the POSIX-style relative path from `root` to the resolved target,
    or None if the path is invalid, escapes the root, or violates any constraints.
    """
    if not isinstance(raw, str) or not raw:
        return None
    if len(raw) > max_len:
        return None
    
    s = raw.strip().replace("\\", "/")
    if not s or any(ch in s for ch in ("\x00", "\n", "\r")):
        return None
        
    root_path = os.path.realpath(os.path.expanduser(str(root)))
    
    if os.path.isabs(s):
        s_real = os.path.realpath(os.path.expanduser(s))
    else:
        if ".." in s.split("/"):
            return None
        s_real = os.path.realpath(os.path.join(root_path, s))
        
    if s_real != root_path and not s_real.startswith(root_path + os.sep):
        return None
        
    if deny_root and s_real == root_path:
        return None
        
    if exts is not None:
        if os.path.splitext(s_real)[1].lower() not in exts:
            return None
            
    if must_exist and not os.path.exists(s_real):
        return None
        
    if must_be_dir and not os.path.isdir(s_real):
        return None
        
    if must_be_file and not os.path.isfile(s_real):
        return None
        
    return os.path.relpath(s_real, root_path).replace(os.sep, "/")
