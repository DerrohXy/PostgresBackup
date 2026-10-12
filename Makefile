install-dependencies:
	venv/bin/python3 -m pip install -r requirements.txt

format:
	venv/bin/python3 -m isort .
	venv/bin/python3 -m black .
	venv/bin/python3 -m flake8 .
