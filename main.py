"""Ad Cleaner — entry point (BUILD_PLAN 4.7)."""

import tkinter as tk

from gui import AdCleanerApp


def main():
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.25)  # readable on high-DPI Windows
    except tk.TclError:
        pass
    AdCleanerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
