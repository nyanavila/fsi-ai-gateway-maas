FROM node:20-slim AS builder
WORKDIR /app
COPY package.json .
RUN npm install --legacy-peer-deps
COPY public/ ./public/
COPY src/ ./src/
ARG REACT_APP_GATEWAY_URL
ENV REACT_APP_GATEWAY_URL=$REACT_APP_GATEWAY_URL
RUN npm run build

FROM nginx:alpine AS runtime
COPY --from=builder /app/build /usr/share/nginx/html
RUN chgrp -R 0 /usr/share/nginx/html /var/cache/nginx /var/log/nginx /etc/nginx && \
    chmod -R g=u /usr/share/nginx/html /var/cache/nginx /var/log/nginx /etc/nginx && \
    chown -R 1001:0 /var/cache/nginx /var/log/nginx /etc/nginx
USER 1001
EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]
