from pydantic import BaseModel
from typing import List

class OrderIn(BaseModel):
    symbol: str
    quantity: float
    leverage: int

class OrderRequest(BaseModel):
    account: str
    order: List[OrderIn]
