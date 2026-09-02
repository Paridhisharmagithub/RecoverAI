run:
	python3 -m venv .venv
	. .venv/bin/activate && python -m pip install -r requirements.txt && uvicorn app.main:app --host 127.0.0.1 --port 8000

test:
	PYTHONPATH=. pytest -q tests
