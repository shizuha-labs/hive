FROM nginx:alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html /usr/share/nginx/html/index.html
COPY status.json /usr/share/nginx/html/status.json
COPY incidents.rss /usr/share/nginx/html/incidents.rss
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
