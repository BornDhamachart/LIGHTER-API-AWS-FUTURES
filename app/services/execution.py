from app.schemas.orders import OrderRequest
from typing import Dict, Any
from fastapi import HTTPException
import asyncio
import lighter
from app.settings import settings
import math
import aioboto3
from botocore.exceptions import ClientError, BotoCoreError
import json

BASE_URL = settings.BASE_URL
BATCH_SIZE = 5
DELAY_MS = 2000

async def initialize_aws_secret_manager_async(
    secret_id: str, 
    region_name: str = "ap-northeast-2"
) -> Dict[str, Any]:
    """
    Fetch and parse a secret from AWS Secrets Manager asynchronously.
    Returns parsed JSON as a dict.
    """
    session = aioboto3.Session()
    async with session.client("secretsmanager", region_name=region_name) as client:
        try:
            resp = await client.get_secret_value(SecretId=secret_id)
            secret_str = resp.get("SecretString") or resp.get("SecretBinary")
            if secret_str is None:
                raise ValueError("Secret returned empty SecretString/SecretBinary")

            if isinstance(secret_str, (bytes, bytearray)):
                secret_str = secret_str.decode("utf-8")

            secret_data = json.loads(secret_str)
            return secret_data

        except (ClientError, BotoCoreError) as e:
            raise RuntimeError(f"Failed to fetch secret {secret_id}: {e}") from e

        except json.JSONDecodeError as e:
            raise ValueError(f"Secret {secret_id} is not valid JSON: {e}") from e

async def fetch_account_index(client):
    account_instance = lighter.AccountApi(client)
    data = await account_instance.account(by="l1_address", value=secret_data.get("WALLET_ADDRESS"))
    return data.accounts[0].index

async def fetch_account(client):
    account_instance = lighter.AccountApi(client)
    data = await account_instance.account(by="l1_address", value=secret_data.get("WALLET_ADDRESS"))
    return data

async def fetch_exchange_stats(client):
    order_instance = lighter.OrderApi(client)
    data = await order_instance.exchange_stats()
    return data

async def fetch_order_books(client):
    order_instance = lighter.OrderApi(client)
    data = await order_instance.order_books()
    return data

def build_market_id_map(order_books):
    mapping = {ob.symbol: ob.market_id for ob in order_books}
    return mapping

async def execute_isolated_orders(final_orders, future_account, future_exchange_info, signer_client, call_delay: float = 1.0):
    market_id_map = build_market_id_map(future_exchange_info.order_books)

    for order in final_orders:
        # Find existing position
        pos = next(
            (p for p in future_account.accounts[0].positions if p.symbol == order["symbol"]),
            None,
        )
        market_id = market_id_map.get(order["symbol"])

        if market_id is None:
            raise HTTPException(
                status_code=400,
                detail=f"Market ID not found for {order['symbol']} "
                f"(keys={list(market_id_map.keys())})"
            )

        # Only switch if not already isolated
        if pos and pos.margin_mode != signer_client.ISOLATED_MARGIN_MODE:
            current_leverage = int(100 / float(pos.initial_margin_fraction))
            try:
                lev_tx, response, err = await signer_client.update_leverage(
                    leverage=current_leverage,   # keep leverage the same
                    margin_mode=signer_client.ISOLATED_MARGIN_MODE,
                    market_index=market_id,
                )

                if err:
                    raise HTTPException(
                        status_code=502,
                        detail=f"Error switching to ISOLATED for {order['symbol']}: {err}"
                    )

            except Exception as e:
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to set ISOLATED for {order['symbol']}: {str(e)}"
                )

            # avoid API rate-limit issues
            await asyncio.sleep(call_delay)



async def execute_leverage_orders(final_orders, future_account, future_exchange_info, signer_client, call_delay: float = 1.0):
    market_id_map = build_market_id_map(future_exchange_info.order_books)

    for order in final_orders:
        # find position if it exists
        pos = next((p for p in future_account.accounts[0].positions if p.symbol == order["symbol"]), None)
        current_leverage = int(100 / float(pos.initial_margin_fraction)) if pos else None

        market_id = market_id_map.get(order["symbol"])
        if market_id is None:
            raise HTTPException(
                status_code=400,
                detail=f"Market ID not found for {order['symbol']} "
                f"(keys={list(market_id_map.keys())})"
            )
            
        # only adjust if leverage actually needs to change
        if current_leverage != order["leverage"]:
            try:
                lev_tx, response, err = await signer_client.update_leverage(
                    leverage=order["leverage"],
                    margin_mode=signer_client.ISOLATED_MARGIN_MODE,  # force isolated
                    market_index=market_id,
                )

                if err:
                    raise HTTPException(
                        status_code=502,
                        detail=f"Error updating leverage for {order['symbol']}: {err}"
                    )

            except Exception as e:
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to update leverage for {order['symbol']}: {str(e)}"
                )

            # add delay between calls to avoid 429
            await asyncio.sleep(call_delay)

async def execute_final_market_orders(
    final_orders,
    future_exchange_info,
    reduce_only: int,
    signer_client,
    base_index: int = 1000,
    slippage_percent: float = 0.03,  # 3% buffer
    call_delay: float = 0.5,         # delay between orders
):
    """
    Execute final market orders using global signer_client.

    Args:
        final_orders: list of dicts with:
            - symbol, side ("BUY"/"SELL"), type="MARKET"
            - quantity (float, final base amount)
            - marketPrice, sizeDecimals, priceDecimals
        future_exchange_info: exchange info containing order_books
        reduce_only: 1 for closing positions, 0 for opening/adjusting
        base_index: ensures unique client_order_index across runs
        slippage_percent: acceptable buffer for avg_execution_price
        call_delay: seconds to sleep between calls (avoid 429s)
    """
    if not final_orders:
        return []

    responses = []
    market_id_map = build_market_id_map(future_exchange_info.order_books)

    for i, order in enumerate(final_orders):
        market_id = market_id_map.get(order["symbol"])
        if market_id is None:
            raise HTTPException(
                status_code=400,
                detail=f"Market ID not found for {order['symbol']} "
                       f"(keys={list(market_id_map.keys())})"
            )

        size_decimals = int(order["sizeDecimals"])
        price_decimals = int(order["priceDecimals"])

        # scale to integers for Lighter
        base_amount = int(round(float(order["quantity"]) * (10 ** size_decimals)))
        worst_price = float(order["marketPrice"]) * (
            1 + slippage_percent if order["side"].upper() == "BUY" else 1 - slippage_percent
        )
        avg_execution_price = int(round(worst_price * (10 ** price_decimals)))
        is_ask = order["side"].upper() == "SELL"

        try:
            tx = await signer_client.create_market_order(
                market_index=market_id,
                client_order_index=base_index + i,
                base_amount=base_amount,
                avg_execution_price=avg_execution_price,
                is_ask=is_ask,
            )

            if not tx:
                raise HTTPException(
                    status_code=502,
                    detail=f"Lighter SDK returned None for {order['symbol']}"
                )

            print(
                f"✅ Market order {order['side']} {order['symbol']} "
                f"qty={order['quantity']} reduce_only={reduce_only}, "
                f"slippage={slippage_percent*100:.1f}%"
            )
            responses.append({"order": order, "response": tx})

        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to execute order for {order['symbol']}: {str(e)}"
            )

        # avoid hitting rate limits
        if i + 1 < len(final_orders):
            await asyncio.sleep(call_delay)

    return responses



async def execute_order(args: OrderRequest) -> Dict[str, Any]:
    """
    Adapted logic for Lighter, similar steps as your Binance version.
    """
    print("args:", args)
    
    client = lighter.ApiClient(
    configuration=lighter.Configuration(host=BASE_URL)
    )
    
    try:
        secret_data = await initialize_aws_secret_manager_async(
            secret_id=args.account, region_name="ap-northeast-2"
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch secret: {str(e)}")
    try:
        account_index = await fetch_account_index(client)
       
        if not account_index:
            raise ValueError("Invalid account index response")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch account index: {str(e)}")
    
    signer_client = lighter.SignerClient(
        url=BASE_URL,
        private_key=secret_data.get("PRIVATE_KEY"),
        account_index=account_index,
        api_key_index=secret_data.get("API_KEY_INDEX"),
    )

    # 1) Input validation
    total_quantity = sum(abs(o.quantity) for o in args.order)
    if total_quantity > 1:
        raise HTTPException(status_code=400, detail="Sum input order > 100%")
    for o in args.order:
        if o.leverage > 5:
            raise HTTPException(status_code=400, detail="Leverage factor can't be more than 5")

    # 2) Fetch exchange stats
    try:
        resp = await fetch_exchange_stats(client)
        if not resp or not resp.order_book_stats:
            raise ValueError("Invalid market price response")
        future_market_price = {stat.symbol: float(stat.last_trade_price) for stat in resp.order_book_stats}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch future market price: {str(e)}")

    # 3) Fetch exchange info
    try:
        future_exchange_info = await fetch_order_books(client)
        if not future_exchange_info:
            raise ValueError("Invalid exchange info response")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch future exchange info: {str(e)}")

    # 4) Fetch account
    try:
        future_account = await fetch_account(client)
        if not future_account:
            raise ValueError("Invalid future account response")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch future account: {str(e)}")

    total_margin_balance = float(future_account.accounts[0].total_asset_value) * 0.98
    if total_margin_balance == 0:
        raise HTTPException(status_code=400, detail="Total margin balance is 0")

    # 5) Current open positions
    positions = future_account.accounts[0].positions
    current_open_positions = [pos for pos in positions if float(pos.position) != 0]
    current_open_position_percentage = []
    for pos in current_open_positions:
        market_price = float(future_market_price[pos.symbol])
        leverage = int(100 / float(pos.initial_margin_fraction))
        signed_quantity = float(pos.position) * (1 if pos.sign == 1 else -1)
        current_open_position_percentage.append({
            "symbol": pos.symbol,
            "quantity": signed_quantity,
            "quantityPercentage": (signed_quantity * market_price) / leverage / total_margin_balance,
            "leverage": leverage,
        })
    print("current_open_position_percentage:", current_open_position_percentage)
    
    # 6) Build order summary
    order_part_one, order_part_two = [], []

    for current_position in current_open_position_percentage:
        new_position = next((np for np in args.order if np.symbol == current_position["symbol"]), None)
        market_price = round(float(future_market_price[current_position["symbol"]]), 8)
        order_book = next((ob for ob in future_exchange_info.order_books if ob.symbol == current_position["symbol"]), None)
        min_order_size = float(order_book.min_base_amount)
        min_notional_size = float(order_book.min_quote_amount)
        step_size = 1 / (10 ** order_book.supported_size_decimals)
        size_decimals = order_book.supported_size_decimals
        price_decimals = order_book.supported_price_decimals

        if not new_position or new_position.quantity == 0:
            order_part_one.append({
                "symbol": current_position["symbol"],
                "percentage": -current_position["quantityPercentage"],
                "usdAmount": -current_position["quantityPercentage"] * total_margin_balance,
                "coinAmount": -current_position["quantity"],
                "minOrderSize": min_order_size,
                "minNotionalSize": min_notional_size,
                "stepSize": step_size,
                "sizeDecimals": size_decimals,
                "priceDecimals": price_decimals,
                "marketPrice": market_price,
                "leverage": current_position["leverage"],
                "closePosition": "Y",
                "executeFirst": 0,
            })
        else:
            usd_amount = (
                new_position.quantity * total_margin_balance * new_position.leverage
                - current_position["quantityPercentage"] * total_margin_balance * current_position["leverage"]
            )
            coin_amount = (
                ((new_position.quantity * total_margin_balance) / market_price) * new_position.leverage
                - ((current_position["quantityPercentage"] * total_margin_balance) / market_price) * current_position["leverage"]
            )
            execute_first = 1 if abs(((current_position["quantityPercentage"] * total_margin_balance) / market_price) * current_position["leverage"]) - abs(((new_position.quantity * total_margin_balance) / market_price) * new_position.leverage) > 0 else 0
            order_part_one.append({
                "symbol": current_position["symbol"],
                "percentage": new_position.quantity - current_position["quantityPercentage"],
                "usdAmount": usd_amount,
                "coinAmount": coin_amount,
                "minOrderSize": min_order_size,
                "minNotionalSize": min_notional_size,
                "stepSize": step_size,
                "sizeDecimals": size_decimals,
                "priceDecimals": price_decimals,
                "marketPrice": market_price,
                "leverage": new_position.leverage,
                "closePosition": "N",
                "executeFirst": execute_first,
            })
    print("order_part_one:", order_part_one)
    
    for new_position in args.order:
        current_position = next((cp for cp in current_open_position_percentage if cp["symbol"] == new_position.symbol), None)
        if current_position: 
            continue
        market_price = round(float(future_market_price.get(new_position.symbol, 0)), 8)
        order_book = next((ob for ob in future_exchange_info.order_books if ob.symbol == new_position.symbol), None)
        min_order_size = float(order_book.min_base_amount)
        min_notional_size = float(order_book.min_quote_amount)
        step_size = 1 / (10 ** order_book.supported_size_decimals)
        size_decimals = order_book.supported_size_decimals
        price_decimals = order_book.supported_price_decimals
        order_part_two.append({
            "symbol": new_position.symbol,
            "percentage": new_position.quantity,
            "usdAmount": new_position.quantity * total_margin_balance * new_position.leverage,
            "coinAmount": ((new_position.quantity * total_margin_balance) / market_price) * new_position.leverage,
            "minOrderSize": min_order_size,
            "minNotionalSize": min_notional_size,
            "stepSize": step_size,
            "sizeDecimals": size_decimals,
            "priceDecimals": price_decimals,
            "marketPrice": market_price,
            "leverage": new_position.leverage,
            "closePosition": "Y" if new_position.quantity == 0 else "N",
            "executeFirst": 0,
        })
    print("order_part_two:", order_part_two)

    order_summary = order_part_one + order_part_two
    print("order_summary:", order_summary)

    # 7) Adjust leverage + isolated
    try:
        await execute_isolated_orders(order_summary, future_account, future_exchange_info, signer_client)
        await execute_leverage_orders(order_summary, future_account, future_exchange_info, signer_client)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error executing leverage/margin updates: {str(e)}")

    # 8) Final orders
    final_order1 = [
        {
            "symbol": r["symbol"],
            "side": "BUY" if r["coinAmount"] >= 0 else "SELL",
            "type": "MARKET",
            "quantity": round(
                math.floor(round(abs(r["coinAmount"]) / r["stepSize"], 8)) * r["stepSize"],
                r["sizeDecimals"],
            ),
            "marketPrice": r["marketPrice"],
            "sizeDecimals": r["sizeDecimals"],
            "priceDecimals": r["priceDecimals"],
        }
        for r in sorted((order for order in order_summary if abs(order["coinAmount"]) > 0 and order["closePosition"] == "Y"), key=lambda x: x["executeFirst"], reverse=True)
    ]
    print("final_order1:", final_order1)

    final_order2 = [
        {
            "symbol": r["symbol"],
            "side": "BUY" if r["coinAmount"] >= 0 else "SELL",
            "type": "MARKET",
             "quantity": round(
                math.floor(round(abs(r["coinAmount"]) / r["stepSize"], 8)) * r["stepSize"],
                r["sizeDecimals"],
            ),
            "marketPrice": r["marketPrice"],
            "sizeDecimals": r["sizeDecimals"],
            "priceDecimals": r["priceDecimals"],
        }
        for r in sorted(
            (order for order in order_summary
             if (int(abs(order["coinAmount"]) / order["stepSize"]) * order["stepSize"]) >= order["minOrderSize"]
             and (abs(order["coinAmount"]) * order["marketPrice"]) >= order["minNotionalSize"]
             and order["closePosition"] == "N"),
            key=lambda x: x["executeFirst"], reverse=True)
    ]
    print("final_order2:", final_order2)

    # 9) Execute orders
    order1_response, order2_response = [], []
    if final_order1:
        order1_response = await execute_final_market_orders(final_order1, future_exchange_info, reduce_only=1, signer_client=signer_client)
    if final_order2:
        order2_response = await execute_final_market_orders(final_order2, future_exchange_info, reduce_only=0, signer_client=signer_client)

    # 10) Error check
    has_error = any(
        (item and item.get("response") is not None
         and hasattr(item["response"], "code")
         and getattr(item["response"], "code") != 200)
        for item in (order1_response + order2_response)
    )
    if has_error:
        raise HTTPException(
            status_code=502,
            detail={"status": "error", "account": args.account, "result": "One or more orders failed (Lighter API returned an error code)."},
        )

    await signer_client.close()
    
    # ✅ Final response
    return {
        "status": "ok",
        "account": args.account,
        "result": {
            "currentPosition": current_open_position_percentage,
            "allOrderBeforeAdjusted": order_summary,
            "order1AfterAdjusted": final_order1,
            "order2AfterAdjusted": final_order2,
            "lighterOrder1Response": order1_response,
            "lighterOrder2Response": order2_response,
        },
    }
