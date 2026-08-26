"""train.py —— rx-sweep fixture 入口。

用法见 README.md。例：
  python train.py --variant pure_cnn --epochs 1
  python train.py --variant model8 --epochs 1
  python train.py --variant pure_cnn --kd --epochs 1
"""

from utils import train_rx


if __name__ == "__main__":
    import sys
    sys.exit(train_rx.main())
