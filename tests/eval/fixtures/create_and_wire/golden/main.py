import json
import os

from utils import read_config, slugify, truncate


def main():
    config = read_config("config.json")
    title = slugify(config["title"])
    desc = truncate(config["description"])
    print(f"{title}: {desc}")


if __name__ == "__main__":
    main()
