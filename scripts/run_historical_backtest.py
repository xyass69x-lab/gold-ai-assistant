from pathlib import Path
import sys

# Make the repository root importable when this file is executed by path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backtest_data import fetch_backtest_dataset
from app.backtest_mtf import run_backtest_report


if __name__ == "__main__":
    dataset = fetch_backtest_dataset()
    print(run_backtest_report(dataset))
