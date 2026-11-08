import os

# 目录结构定义
structure = {
    "core": [
        "__init__.py",
        "interfaces.py",
        "state.py",
        "event_bus.py",
        "plugin_manager.py",
        "trace_store.py",
        "runtime.py",
        "conversation_adapter.py"
    ],
    "plugins/modelscope_model": [
        "manifest.json",
        "model_plugin.py"
    ],
    "plugins/local_tools": [
        "manifest.json",
        "tool_plugin.py"
    ],
    ".": [
        "test_runtime.py"
    ]
}


def create_project_structure():
    for dir_path, files in structure.items():
        # 创建文件夹
        os.makedirs(dir_path, exist_ok=True)
        # 创建空文件
        for fname in files:
            full_path = os.path.join(dir_path, fname)
            if not os.path.exists(full_path):
                with open(full_path, "w", encoding="utf-8") as f:
                    # py文件留空，json写空对象模板
                    if fname.endswith(".json"):
                        f.write("{\n}\n")
                    else:
                        f.write("")
                print(f"created: {full_path}")
            else:
                print(f"exists skip: {full_path}")
    print("\n✅ 项目目录结构创建完成！")


if __name__ == "__main__":
    create_project_structure()
