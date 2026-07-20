FROM node:24-alpine AS build

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run test && npm run build

FROM nginx:1.29-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=6 \
    CMD wget -q -O - http://127.0.0.1/healthz | grep -q '^ok' \
    && wget -q -O - http://127.0.0.1/backend-healthz | grep -q '"status":"ok"'
