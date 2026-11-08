"""
模型路径自动发现工具
在 data/models 目录下递归查找包含目标关键词的模型目录，
同时兼容两种布局：
1. ModelScope 平铺布局: data/models/bge-m3/config.json
2. HuggingFace 缓存布局: data/models/models/BAAI--bge-m3/snapshots/master/config.json
"""
import os
from pathlib import Path


# 视为"真实模型目录"必须存在的权重文件之一（排除 1_Pooling 等仅含配置的子目录）
_WEIGHT_FILES = (
    "pytorch_model.bin",
    "model.safetensors",
    "model.onnx",
    "pytorch_model.bin.index.json",  # 分片权重索引
)


def find_model_path(keyword: str, search_root: str = "data/models") -> str:
    """
    在 search_root 下递归查找 config.json，其所在目录（或祖先路径）
    包含 keyword、且该目录下存在模型权重文件，即视为命中，
    返回第一个命中目录的绝对路径字符串。

    Args:
        keyword: 模型名关键词（如 "bge-m3"、"bge-reranker"），大小写不敏感
        search_root: 搜索根目录

    Raises:
        FileNotFoundError: 未找到符合条件的模型目录
    """
    root = Path(search_root)
    if not root.exists():
        raise FileNotFoundError(f"搜索根目录不存在: {search_root}")

    # 以 config.json 为锚点向上匹配关键词：
    # HF 缓存布局中 config.json 位于 snapshots/<revision>/ 层，关键词在其祖先目录名里。
    # 必须同时存在权重文件——排除 1_Pooling / 2_Normalize 等 SentenceTransformer 模块子目录
    for config_path in sorted(root.rglob("config.json")):
        parent = config_path.parent
        if keyword.lower() not in str(parent).lower():
            continue
        if any((parent / w).exists() for w in _WEIGHT_FILES):
            return str(parent)

    raise FileNotFoundError(
        f"未找到包含关键词 '{keyword}' 且含权重文件的模型目录。请确认已运行下载脚本。"
    )


def resolve_model_path(keyword: str, default_path: str,
                       env_var: str, search_root: str = "data/models") -> str:
    """统一的三级路径解析：环境变量 → 默认目录 → 自动发现

    Args:
        keyword: 自动发现用的模型名关键词
        default_path: 代码中的默认相对路径（如 "data/models/bge-m3"）
        env_var: 可覆盖路径的环境变量名（如 "EMBEDDING_MODEL_PATH"）
        search_root: 自动发现根目录

    Returns:
        可用模型目录路径字符串
    """
    # 1) 环境变量显式指定优先
    env_path = os.getenv(env_var, "")
    if env_path and Path(env_path).exists():
        return env_path
    # 2) 默认目录存在则直接使用（ModelScope 平铺布局）
    if Path(default_path).exists():
        return default_path
    # 3) 自动发现（兼容 HF 缓存等任意嵌套布局）
    return find_model_path(keyword, search_root)
