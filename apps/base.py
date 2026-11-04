"""What the app drivers share."""


class AppError(RuntimeError):
    """A driver failure whose message was written for the user: it starts
    with "<app>: " and holds no URL or credential."""
