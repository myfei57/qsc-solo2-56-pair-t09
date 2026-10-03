test:
	PYTHONPATH=. python -m pytest -q

run:
	PYTHONPATH=. python -m psdline --config config/psdline.json serve --host 127.0.0.1 --port 8080

demo:
	PYTHONPATH=. python -m psdline --config config/psdline.json demo --fast

shift-demo:
	PYTHONPATH=. python -m psdline --config config/psdline.json shift-demo --deadline 10

deadcode:
	PYTHONPATH=. python tools/deadcode.py psdline
