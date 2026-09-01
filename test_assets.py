"""
test_assets.py

Diagnostic tool to verify that exotic and unlisted OTC pairs 
(USD/SGD, USD/COP, USD/PHP, OMR/CNY, BHD/CNY) are properly 
captured and normalized by signal_parser.py.
"""

from signal_parser import parse_signal

def run_asset_diagnostics():
    test_cases = [
        # 1. Standard format without explicit OTC tag
        "USD/SGD\nentry at 15:25, BUY\nexpiration 5M",
        "USD/COP\nentry at 15:25, SELL\nexpiration 5M",
        "USD/PHP\nentry at 15:25, BUY\nexpiration 5M",
        "OMR/CNY\nentry at 15:25, SELL\nexpiration 5M",
        "BHD/CNY\nentry at 15:25, BUY\nexpiration 5M",
        
        # 2. Standard format with explicit OTC tag
        "USD/SGD OTC\nentry at 15:25, BUY\nexpiration 5M",
        "USD/COP OTC\nentry at 15:25, SELL\nexpiration 5M",
        "USD/PHP OTC\nentry at 15:25, BUY\nexpiration 5M",
        "OMR/CNY OTC\nentry at 15:25, SELL\nexpiration 5M",
        "BHD/CNY OTC\nentry at 15:25, BUY\nexpiration 5M",
        
        # 3. Inline single-line format
        "USDSGD OTC - CALL - 15:25 - M5",
        "BHDCNY - PUT - 15:25 - M5",
        "OMRCNY OTC - CALL - 15:25 - M5"
    ]

    print(f"{'TEST MESSAGE (FIRST LINE)':<32} | {'STATUS':<8} | {'RAW ASSET':<12} | {'NORMALIZED ASSET'}")
    print("=" * 80)

    for raw_msg in test_cases:
        first_line = raw_msg.split("\n")[0]
        signal = parse_signal(raw_msg)
        
        status = "✅ PASS" if signal.is_valid and signal.normalized_asset else "❌ FAIL"
        raw_asset = signal.asset or "None"
        normalized = signal.normalized_asset or (f"Rejected: {signal.error}" if signal.error else "None")
        
        print(f"{first_line:<32} | {status:<8} | {raw_asset:<12} | {normalized}")

if __name__ == "__main__":
    run_asset_diagnostics()