FROM node:22-alpine AS build

WORKDIR /app

RUN corepack enable && corepack prepare yarn@1.22.22 --activate

COPY package.json yarn.lock ./
RUN yarn install --frozen-lockfile

COPY . .

ARG VITE_SERVER_URL=http://localhost:8000
ENV VITE_SERVER_URL=$VITE_SERVER_URL

RUN yarn build

FROM nginx:1.29-alpine

ENV BACKEND_UPSTREAM=http://backend:8000

COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 5173
