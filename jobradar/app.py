from __future__ import annotations

from .gui import JobRadarApp


def main() -> None:
    app = JobRadarApp()
    app.mainloop()


if __name__ == "__main__":
    main()
