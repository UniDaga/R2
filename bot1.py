import json
import time
import os
import random
from dotenv import load_dotenv
from web3 import Web3
from decimal import Decimal
from eth_abi import encode
from rich.console import Console
from colorama import Fore, Style
from requests import Session

console = Console()

# ─────────────── Load Configs ───────────────
try:
    with open("network_config.json") as f:
        config = json.load(f)
except Exception as e:
    console.print(f"[red]❌ Failed to load config: {str(e)}[/red]")
    exit()

CHAIN_ID = config["chain_id"]
TOKEN_MAPPING = {k: Web3.to_checksum_address(v["address"]) for k, v in config["tokens"].items()}
STAKING_CONTRACT = Web3.to_checksum_address(config["staking_contract"])

try:
    with open("token_abi.json") as f:
        erc20_abi = json.load(f)
    with open("router_swap_abi.json") as f:
        router_swap_abi = json.load(f)
except Exception as e:
    console.print(f"[red]❌ Failed to load ABI JSON file: {str(e)}[/red]")
    exit()

ERC20_ABI = [
    {"constant": True,"inputs": [{"name": "_owner","type": "address"}],
     "name": "balanceOf","outputs": [{"name": "balance","type": "uint256"}],"type": "function"},
    {"constant": True,"inputs": [{"name": "_owner","type": "address"}, {"name": "_spender","type": "address"}],
     "name": "allowance","outputs": [{"name": "","type": "uint256"}],"type": "function"},
    {"constant": False,"inputs": [{"name": "_spender","type": "address"}, {"name": "_value","type": "uint256"}],
     "name": "approve","outputs": [{"name": "","type": "bool"}],"type": "function"}
]

# ─────────────── Proxy Functions ───────────────
def build_web3_with_proxy(rpc_url, proxy_url=None, timeout=25):
    session = Session()
    if proxy_url:
        session.proxies.update({
            "http": proxy_url,
            "https": proxy_url
        })
    provider = Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": timeout, "session": session})
    w3 = Web3(provider)
    return w3

def load_proxies_from_env():
    proxies = []
    i = 1
    while True:
        val = os.getenv(f"PROXY_URL_{i}")
        if not val:
            break
        proxies.append(val.strip())
        i += 1
    return proxies

def pick_proxy(proxies, wallet_idx, round_idx, strategy="per_wallet"):
    if not proxies:
        return None
    if strategy == "per_round":
        return proxies[(round_idx - 1) % len(proxies)]
    return proxies[(wallet_idx - 1) % len(proxies)]

def get_working_web3(rpc_url, proxies, start_index=0):
    if not proxies:
        w3 = build_web3_with_proxy(rpc_url, None)
        return w3 if w3.is_connected() else None, None
    n = len(proxies)
    for i in range(n):
        proxy = proxies[(start_index + i) % n]
        w3 = build_web3_with_proxy(rpc_url, proxy)
        if w3.is_connected():
            return w3, proxy
    # fallback direct
    w3 = build_web3_with_proxy(rpc_url, None)
    return (w3, None) if w3.is_connected() else (None, None)

def set_global_web3(w3):
    global web3
    web3 = w3

# ─────────────── Utility Functions ───────────────
nonce_tracker = {}
def short(addr): return f"{addr[:6]}...{addr[-4:]}"
def reset_nonce_tracker(): global nonce_tracker; nonce_tracker = {}
def get_gas_price(): return int(web3.eth.gas_price * Decimal(2))
def get_gas(): return web3.eth.gas_price + Web3.to_wei(5, 'gwei')
def get_erc20(address): return web3.eth.contract(address=address, abi=erc20_abi)
def tx_delay(): time.sleep(2)
def get_managed_nonce(addr):
    global nonce_tracker
    blockchain_nonce = web3.eth.get_transaction_count(addr, "pending")
    if addr not in nonce_tracker:
        nonce_tracker[addr] = blockchain_nonce
    else:
        nonce_tracker[addr] = max(nonce_tracker[addr] + 1, blockchain_nonce)
    return nonce_tracker[addr]

def show_status(action, sender, contract, status, tx_hash=None): 
    if tx_hash:
        console.print(f"🔗 TX Hash    https://pharos-testnet.socialscan.io/tx/0x{tx_hash}")
    console.print("─" * 50)

# ─────────────── Token & Staking ───────────────
def approve_token_swap(sender, spender, amount, privkey, token_addr, label):
    contract = get_erc20(token_addr)
    allowance = contract.functions.allowance(sender, spender).call()
    if allowance >= amount:
        return True

    tx = contract.functions.approve(spender, amount).build_transaction({
        "from": sender,
        "nonce": get_managed_nonce(sender),
        "gasPrice": get_gas(),
        "chainId": CHAIN_ID,
        "gas": 600000
    })
    signed = web3.eth.account.sign_transaction(tx, privkey)
    tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
    show_status(f"Approve {label}", sender, token_addr, "[yellow]Submitted[/yellow]", web3.to_hex(tx_hash))
    tx_delay()
    return True

def approve_token_stake(token_addr, owner, spender, amount, key):
    contract = web3.eth.contract(address=token_addr, abi=ERC20_ABI)
    tx = contract.functions.approve(spender, amount).build_transaction({
        'chainId': CHAIN_ID,
        'from': owner,
        'nonce': get_managed_nonce(owner),
        'gas': 600000,
        'gasPrice': get_gas_price()
    })
    signed = web3.eth.account.sign_transaction(tx, key)
    tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
    tx_delay()

def stake_r2usd_to_sr2usd(sender, privkey, amount):
    token = web3.eth.contract(address=TOKEN_MAPPING["R2USD"], abi=ERC20_ABI)
    balance = token.functions.balanceOf(sender).call()
    stake_amount = min(balance, amount)  
    if stake_amount == 0:
        console.print(f"[yellow]⚠️ No R2USD available for staking[/yellow]")
        return None

    allowance = token.functions.allowance(sender, STAKING_CONTRACT).call()
    if allowance < stake_amount:
        approve_token_stake(TOKEN_MAPPING["R2USD"], sender, STAKING_CONTRACT, stake_amount, privkey)

    data = bytes.fromhex("1a5f0f00") + encode(["uint256"] * 10, [stake_amount] + [0]*9)
    tx = {
        'chainId': CHAIN_ID,
        'from': sender,
        'to': STAKING_CONTRACT,
        'nonce': get_managed_nonce(sender),
        'gasPrice': get_gas_price(),
        'gas': 600000,
        'data': data
    }
    signed = web3.eth.account.sign_transaction(tx, privkey)
    tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
    console.print(f"[green]✅ Staked {stake_amount / 1e6} R2USD → sR2USD[/green]")
    tx_delay()
    return tx_hash

# ─────────────── Main Function ───────────────
def slow(str, t):
    for char in str:
        print(char, end='', flush=True)
        time.sleep(t / 1000000)

def main():
    text = (f"{Fore.GREEN}     ######### PHAROS R2 TASK BOT ######### {Style.RESET_ALL}")
    slow(text, 30000)
    reset_nonce_tracker()
    load_dotenv()

    wallets = []
    i = 1
    while True:
        key = os.getenv(f"PRIVATE_KEY_{i}")
        if key is None: break
        wallets.append(key)
        i += 1
    
    if not wallets:
        console.print("[red]❌ No private keys found in .env file![/red]")
        return

    proxies = load_proxies_from_env()
    strategy = (os.getenv("PROXY_STRATEGY") or "per_wallet").strip().lower()
    if strategy not in ("per_wallet", "per_round"):
        strategy = "per_wallet"

    RPC_URL = config["rpc"]

    for i, pk in enumerate(wallets, 1):
        acc = Web3().eth.account.from_key(pk)
        sender = acc.address
        console.print(f"\n[bold cyan]▶ Wallet {i}: {short(sender)}[/bold cyan]")
        console.print("─" * 50)

        initial_proxy = pick_proxy(proxies, wallet_idx=i, round_idx=1, strategy=strategy)
        w3, active_proxy = get_working_web3(RPC_URL, proxies, start_index=(proxies.index(initial_proxy) if (proxies and initial_proxy in proxies) else 0) if proxies else 0)
        if not w3:
            console.print("[red]❌ No working RPC (even without proxy). Skip wallet.[/red]")
            continue

        set_global_web3(w3)
        console.print(f"[green]✅ Using Proxy:[/green] {active_proxy or 'DIRECT'}")

        token_usdc = TOKEN_MAPPING["USDC"]
        token_r2usd = TOKEN_MAPPING["R2USD"]

        for round_num in range(1, 96):
            if strategy == "per_round":
                desired_proxy = pick_proxy(proxies, wallet_idx=i, round_idx=round_num, strategy=strategy)
                if desired_proxy != active_proxy:
                    w3_new, active_proxy_new = get_working_web3(RPC_URL, proxies, start_index=(proxies.index(desired_proxy) if (proxies and desired_proxy in proxies) else 0) if proxies else 0)
                    if w3_new:
                        set_global_web3(w3_new)
                        active_proxy = active_proxy_new
                        console.print(f"[blue]🔁 Rotated proxy →[/blue] {active_proxy or 'DIRECT'}")

            console.print(f"\n[bold yellow]🔁 Round {round_num}[/bold yellow]")
            try:
                random_amount = round(random.uniform(0.1, 1.0), 2)
                amount_usdc = int(Decimal(str(random_amount)) * 10**config["tokens"]["USDC"]["decimals"])
                console.print(f"[cyan]💱 Swap amount (USDC): {random_amount}[/cyan]")

                approve_token_swap(sender, token_r2usd, amount_usdc, pk, token_usdc, "USDC")
                func_selector = bytes.fromhex("095e7a95")
                encoded_args = encode(
                    ['address', 'uint256', 'uint256', 'uint256', 'uint256', 'uint256', 'uint256'],
                    [sender, amount_usdc, 0, 0, 0, 0, 0]
                )
                data = func_selector + encoded_args
                tx = {
                    'chainId': CHAIN_ID,
                    'from': sender,
                    'to': token_r2usd,
                    'data': web3.to_hex(data),
                    'gasPrice': get_gas(),
                    'nonce': get_managed_nonce(sender),
                    'gas': 600000
                }
                signed_tx = web3.eth.account.sign_transaction(tx, pk)
                tx_hash = web3.eth.send_raw_transaction(signed_tx.raw_transaction)

                receipt = web3.eth.wait_for_transaction_receipt(tx_hash)
                console.print(f"🔗 TX Hash (Swap) https://pharos-testnet.socialscan.io/tx/0x{tx_hash.hex()}")

                amount_r2usd = int(Decimal(str(random_amount)) * 10**config["tokens"]["R2USD"]["decimals"])
                console.print(f"[magenta]📥 Stake amount (R2USD): {random_amount}[/magenta]")
                tx_hash2 = stake_r2usd_to_sr2usd(sender, pk, amount_r2usd)
                if tx_hash2:
                    console.print(f"🔗 TX Hash (Stake) https://pharos-testnet.socialscan.io/tx/0x{tx_hash2.hex()}")

                time.sleep(3)

            except Exception as e:
                console.print(f"[red]⚠️ RPC/Tx error: {e}[/red]")
                time.sleep(2)
                continue

# ─────────────── Run Bot ───────────────
if __name__ == "__main__":
    main()
