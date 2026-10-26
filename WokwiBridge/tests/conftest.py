"""Os testes do bridge nunca leem o WokwiBridge/.env real (a configuracao vem do monkeypatch em cada teste)."""
import os

os.environ["FLOW_SEM_DOTENV"] = "1"
