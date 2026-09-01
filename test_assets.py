"""
test_assets.py

Diagnostic tool to verify complex Telegram signal formats, including 
multi-level martingale, timezone offsets, and split messages.
"""

from signal_parser import parse_signal

def run_asset_diagnostics():
    test_cases = [
        """📊 🇪🇺 EUR/HUF 🇭🇺 OTC
🕘 Expiration 5M
⏺️ Entry at 06:30
🟩 BUY

🔽 Martingale levels 
1️⃣ level At 06:35
2️⃣ level At 06:40
3️⃣ level At 06:45""",

        """📊 FREE SIGNALS 📊
⏰ Time Zone: UTC -3

• EURJPY-OTC - PUT 🟥 - 07:05
• Expiration: 5 minutes (M5)
• If you lose, make up to 2 Gale's.""",

        """AUD/CAD OTC M2
[01/09/2026 13:02] Pocket Option inside VIP: 🔽DOWN🔽"""
    ]

    print(f"{'TEST CASE (1st LINE)':<25} | {'STATUS':<7} | {'ASSET':<11} | {'DIR':<5} | {'ENTRY':<6} | {'EXP':<4} | {'MARTINGALES'}")
    print("-" * 95)

    for raw_msg in test_cases:
        # Extract the first line to keep the console output clean
        first_line = raw_msg.strip().split("\n")[0]
        if len(first_line) > 22:
            first_line = first_line[:19] + "..."
            
        signal = parse_signal(raw_msg)
        
        status = "✅ PASS" if signal.is_valid else "❌ FAIL"
        asset = signal.normalized_asset or str(signal.asset)
        direction = str(signal.direction).upper() if signal.direction else "N/A"
        entry = str(signal.entry_time) if signal.entry_time else ("NOW" if signal.immediate else "N/A")
        exp = str(signal.expiration_minutes) if signal.expiration_minutes else "N/A"
        gales = ", ".join(signal.martingale_times) if signal.has_martingale else "None"
        
        print(f"{first_line:<25} | {status:<7} | {asset:<11} | {direction:<5} | {entry:<6} | {exp:<4} | {gales}")

if __name__ == "__main__":
    run_asset_diagnostics()