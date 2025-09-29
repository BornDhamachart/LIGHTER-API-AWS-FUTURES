import asyncio
from fastapi import APIRouter, Depends, HTTPException
from app.deps.auth import auth_token
from app.services.execution import execute_order
from app.schemas.orders import OrderRequest
import json
from app.settings import settings
import httpx

router = APIRouter(tags=["orders"])

LINE_BOT_TOKEN = settings.LINE_BOT_TOKEN
TARGET_IDS = settings.TARGET_IDS.split(",")

async def alert_to_line_bot(message: str):
    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "Authorization": f"Bearer {LINE_BOT_TOKEN}",
    }

    async with httpx.AsyncClient() as client:
        tasks = []
        for target_id in TARGET_IDS:
            data = {
                "to": target_id,
                "messages": [
                    {
                        "type": "text",
                        "text": message,
                    },
                ],
            }
            tasks.append(
                client.post("https://api.line.me/v2/bot/message/push", headers=headers, json=data)
            )

        responses = await asyncio.gather(*tasks, return_exceptions=True)

        for target_id, resp in zip(TARGET_IDS, responses):
            if isinstance(resp, Exception):
                print(f"❌ Error sending to {target_id}: {resp}")
            elif resp.status_code != 200:
                print(f"❌ LINE error for {target_id}: {resp.text}")
            else:
                print(f"✅ LINE response for {target_id}: {resp.json()}")

# --- Controller ---
@router.post("/executeOrder")
async def execute_order_with_retry(
    payload: OrderRequest,
    user=Depends(auth_token),
):
    MAX_RETRIES = 3
    DELAY_SEC = 3

    retry_count = 0
    while retry_count < MAX_RETRIES:
        print(f"Attempt {retry_count + 1} of {MAX_RETRIES}")

        try:
            # Execute core logic
            result = await execute_order(payload)

            # ✅ success alert
            await alert_to_line_bot(
                json.dumps(
                    {
                        "type": "Lighter futures",
                        "status": result.get("status"),
                        "account": result.get("account"),
                        "attempt": retry_count + 1,
                    }
                )
            )

            print("✅ Order executed successfully")
            return {
                "type": "Lighter futures",
                "status": result.get("status"),
                "account": result.get("account"),
                "result": result.get("result"),
                "attempt": retry_count + 1,
            }

        except HTTPException as e:
            # known error from inner logic
            print(f"❌ HTTP error on attempt {retry_count+1}: {e.detail}")

            error_payload = {
                "type": "Lighter futures",
                "status": "error",
                "account": getattr(payload, "account", "unknown"),
                "result": e.detail,
                "attempt": retry_count + 1,
            }

            await alert_to_line_bot(json.dumps(error_payload))

            if retry_count == MAX_RETRIES - 1:
                # ✅ final fail
                await alert_to_line_bot(
                    json.dumps(
                        {
                            "type": "Lighter futures",
                            "status": "error",
                            "account": error_payload["account"],
                            "message": "Max retries reached",
                        }
                    )
                )
                raise e  # preserve original HTTPException

        except Exception as e:
            # unexpected error
            print(f"❌ Unexpected error on attempt {retry_count+1}:", str(e))

            error_payload = {
                "type": "Lighter futures",
                "status": "error",
                "account": getattr(payload, "account", "unknown"),
                "result": str(e),
                "attempt": retry_count + 1,
            }

            await alert_to_line_bot(json.dumps(error_payload))

            if retry_count == MAX_RETRIES - 1:
                await alert_to_line_bot(
                    json.dumps(
                        {
                            "type": "Lighter futures",
                            "status": "error",
                            "account": error_payload["account"],
                            "message": "Max retries reached",
                        }
                    )
                )
                raise HTTPException(status_code=500, detail=error_payload)

        # retry if not last
        if retry_count < MAX_RETRIES - 1:
            print(f"⏳ Waiting {DELAY_SEC} seconds before retrying...")
            await asyncio.sleep(DELAY_SEC)

        retry_count += 1

