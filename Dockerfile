FROM archlinux:latest

RUN pacman -Syu --noconfirm && \
    pacman -S --noconfirm python android-tools docker docker-compose cronie aria2 git curl tar procps-ng tzdata && \
    pacman -Scc --noconfirm

RUN mkdir -p /opt/arknights && ln -sfn /app /opt/arknights/arknights

WORKDIR /app

ENTRYPOINT ["/app/entrypoint.sh"]
