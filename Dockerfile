FROM python:3.12-slim
RUN useradd --system --uid 10001 --create-home observer
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .
USER observer
ENV BOLT_ON_DATABASE=/state/observer.db BOLT_ON_LISTEN=0.0.0.0
EXPOSE 8080
CMD ["gunicorn","-b","0.0.0.0:8080","reticulum_bolt_on.app:create_app()"]

