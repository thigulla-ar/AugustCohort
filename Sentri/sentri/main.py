import uvicorn


def main() -> None:
    uvicorn.run("sentri.api:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()
