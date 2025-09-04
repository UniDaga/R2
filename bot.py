import json
import time
import os
import random
import requests
from dotenv import load_dotenv
from web3 import Web3
from decimal import Decimal
from eth_abi import encode
from rich.console import Console
from colorama import Fore, Style

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

# ─────────────── Proxy Loader ───────────────
def load_proxies_from_file():
    proxies = []
    try:
        with open("proxy.txt", "r") as f:
            for line in f:
                proxy = line.strip()
                if proxy and not proxy.startswith("#"):
                    # Auto add http:// prefix if missing
                    if not proxy.startswith("http"):
                        proxy = "http://" + proxy
                    proxies.append(proxy)
    except FileNotFoundError:
        console.print("[yellow]⚠️ proxy.txt not found. Using direct connection.[/yellow]")
    return proxies

# ─────────────── Proxy Speed Tester ───────────────
def test_proxy_speed(proxy_url, rpc_url):
    try:
        start = time.time()
        response = requests.post(
            rpc_url,
            json={"jsonrpc":"2.0","method":"web3_clientVersion","params":[],"id":1},
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=3
        )
        if response.status_code == 200:
            return time.time() - start
        return None
    except:
        return None

def find_fastest_proxy(proxies, rpc_url):
    console.print("[cyan]🔍 Testing proxies speed, please wait...[/cyan]")
    proxy_speeds = []
    for proxy in proxies:
        speed = test_proxy_speed(proxy, rpc_url)
        if speed:
            proxy_speeds.append((proxy, speed))
            console.print(f"[green]✅ Working Proxy:[/green] {proxy} → {round(speed,2)}s")
        else:
            console.print(f"[red]❌ Dead Proxy:[/red] {proxy}")
    if not proxy_speeds:
        return None
    # Sort by latency, fastest first
    proxy_speeds.sort(key=lambda x: x[1])
    console.print(f"[blue]⚡ Fastest Proxy Selected →[/blue] {proxy_speeds[0][0]}")
    return proxy_speeds[0][0]

# ─────────────── Web3 Proxy Builder ───────────────
def build_web3_with_proxy(rpc_url, proxy_url=None, timeout=25):
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    provider = Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": timeout, "proxies": proxies})
    return Web3(provider)

def get_working_web3(rpc_url, proxies, current_index=0):
    if not proxies:
        w3 = build_web3_with_proxy(rpc_url, None)
        return w3 if w3.is_connected() else None, None
    n = len(proxies)
    for i in range(n):
        proxy = proxies[(current_index + i) % n]
        w3 = build_web3_with_proxy(rpc_url, proxy)
        if w3.is_connected():
            return w3, proxy
    return build_web3_with_proxy(rpc_url, None), None

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

# ─────────────── Token Approve & Stake ───────────────
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
    tx_delay()
    return True

def stake_r2usd_to_sr2usd(sender, privkey, amount):
    token = web3.eth.contract(address=TOKEN_MAPPING["R2USD"], abi=erc20_abi)
    balance = token.functions.balanceOf(sender).call()
    stake_amount = min(balance, amount)
    if stake_amount == 0:
        console.print(f"[yellow]⚠️ No R2USD available for staking[/yellow]")
        return None

    allowance = token.functions.allowance(sender, STAKING_CONTRACT).call()
    if allowance < stake_amount:
        approve_token_swap(sender, STAKING_CONTRACT, stake_amount, privkey, TOKEN_MAPPING["R2USD"], "R2USD")

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
def main():
    text = (f"{Fore.GREEN}     ######### PHAROS R2 TASK BOT ######### {Style.RESET_ALL}")
    for c in text:
        print(c, end='', flush=True)
        time.sleep(0.00002)
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

    proxies = load_proxies_from_file()
    console.print(f"[blue]Loaded Proxies:[/blue] {len(proxies)} found → {proxies}")

    RPC_URL = config["rpc"]

    # 🔹 Find fastest proxy if any available
    fastest_proxy = find_fastest_proxy(proxies, RPC_URL)
    if fastest_proxy:
        console.print(f"[green]✅ Using Fastest Proxy:[/green] {fastest_proxy}")
    else:
        console.print(f"[yellow]⚠️ No working proxy found, using DIRECT RPC[/yellow]")

    for i, pk in enumerate(wallets, 1):
        acc = Web3().eth.account.from_key(pk)
        sender = acc.address
        console.print(f"\n[bold cyan]▶ Wallet {i}: {short(sender)}[/bold cyan]")
        console.print("─" * 50)

        # Try fastest proxy first
        w3, active_proxy = get_working_web3(RPC_URL, [fastest_proxy] if fastest_proxy else proxies)
        if not w3:
            console.print("[red]❌ No working RPC at all. Skipping wallet.[/red]")
            continue

        set_global_web3(w3)
        console.print(f"[green]✅ Using Proxy:[/green] {active_proxy or 'DIRECT'}")

        token_usdc = TOKEN_MAPPING["USDC"]
        token_r2usd = TOKEN_MAPPING["R2USD"]

        for round_num in range(1, 110):
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
