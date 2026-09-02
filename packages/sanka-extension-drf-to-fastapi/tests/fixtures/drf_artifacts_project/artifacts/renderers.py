from rest_framework.renderers import JSONRenderer


class ApiJSONRenderer(JSONRenderer):
    media_type = "application/vnd.sanka.file+json"
    format = "api"
