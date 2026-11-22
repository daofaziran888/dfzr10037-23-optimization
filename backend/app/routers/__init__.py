"""业务路由汇总：固定路径的覆盖段路由优先注册，再注册通用模块资源路由。"""
from __future__ import annotations

from app.routers import index_coverage, resource

ROUTERS = [index_coverage, resource]
