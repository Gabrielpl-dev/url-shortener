.PHONY: run test lint

run:
	./serve --port 8080

test:
	python3 -m unittest discover -s tests -v

lint:
	ruff check .
