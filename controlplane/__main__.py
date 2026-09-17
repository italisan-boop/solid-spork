from controlplane.server import create_controlplane_app
from controlplane.settings import ControlPlaneSettings


def main() -> None:
    settings = ControlPlaneSettings.from_environment()
    app = create_controlplane_app(settings)
    app.run(host=settings.host, port=settings.port, debug=False)


if __name__ == "__main__":
    main()
