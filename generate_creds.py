import os
from py_clob_client.client import ClobClient
from dotenv import load_dotenv

def main():
    # Load environment variables
    load_dotenv()
    
    private_key = os.getenv("PRIVATE_KEY")
    if not private_key:
        print("Error: PRIVATE_KEY not found in .env file.")
        print("Please add 'PRIVATE_KEY=your_wallet_private_key' to your .env file.")
        return

    print("Deriving L2 API credentials from your private key...")
    try:
        client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,  # Polygon mainnet
            key=private_key
        )

        credentials = client.create_or_derive_api_creds()
        
        print("\nSuccess! Here are your credentials:")
        print("=" * 40)
        print(f"POLYMARKET_API_KEY={credentials.api_key}")
        print(f"POLYMARKET_API_SECRET={credentials.api_secret}")
        print(f"POLYMARKET_API_PASSPHRASE={credentials.api_passphrase}")
        print("=" * 40)
        print("\nPlease copy these three lines and replace the existing API credentials in your .env file.")
        print("After saving, you can run 'python main.py' again.")
        
    except Exception as e:
        print(f"\nFailed to derive credentials: {e}")

if __name__ == "__main__":
    main()
