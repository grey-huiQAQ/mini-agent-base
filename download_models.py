from modelscope import snapshot_download

def download():
    print("下载 bge-m3 ...")
    snapshot_download(
        'BAAI/bge-m3',
        local_dir='data/models/bge-m3',          # 直接指定最终目录
    )
    print("下载 bge-reranker-base ...")
    snapshot_download(
        'BAAI/bge-reranker-base',
        local_dir='data/models/bge-reranker-base',
    )
    print("全部下载完成")

if __name__ == "__main__":
    download()