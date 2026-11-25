from .database import Base, engine
from . import models  # noqa: F401


if __name__ == "__main__":
    Base.metadata.create_all(engine)
    print("Initialized Clearcoat tables")

