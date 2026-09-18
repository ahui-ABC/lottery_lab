"""足彩预测工具 CLI 入口（命令由后续任务逐步添加）。"""
import argparse


def main() -> None:
    parser = argparse.ArgumentParser(prog="football_lottery", description="足彩预测工具")
    # 占位：各阶段任务会陆续挂子命令
    parser.add_argument("--version", action="version", version="0.1.0")
    parser.parse_args()


if __name__ == "__main__":
    main()
