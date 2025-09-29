from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.orders import router as order_router


app = FastAPI(title="Lighter Future API", version="0.1.0")

# CORS (adjust origins for production)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(order_router)

@app.get("/")
def health():
    return "Hello world"
