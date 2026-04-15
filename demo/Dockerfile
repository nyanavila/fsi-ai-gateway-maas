FROM node:20-slim AS builder
WORKDIR /app
COPY package.json vite.config.js index.html ./
RUN npm install
COPY src/ ./src/
ARG REACT_APP_GATEWAY_URL
ENV VITE_GATEWAY_URL=$REACT_APP_GATEWAY_URL
RUN npm run build

FROM nginx:alpine AS runtime
COPY --from=builder /app/build /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
RUN sed -i 's/listen\s*80;/listen 8080;/g' /etc/nginx/conf.d/default.conf && \
    sed -i '/user\s*nginx/d' /etc/nginx/nginx.conf && \
    sed -i 's|pid\s*/run/nginx.pid;|pid /tmp/nginx.pid;|g' /etc/nginx/nginx.conf && \
    chgrp -R 0 /usr/share/nginx/html /var/cache/nginx /var/log/nginx /etc/nginx && \
    chmod -R g=u /usr/share/nginx/html /var/cache/nginx /var/log/nginx /etc/nginx
USER 1001
EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]
