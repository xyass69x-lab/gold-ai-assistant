from app.backtest_data import fetch_backtest_dataset
from app.backtest_mtf import run_backtest_report


if __name__ == "__main__":
    dataset = fetch_backtest_dataset()
    print(run_backtest_report(dataset))
