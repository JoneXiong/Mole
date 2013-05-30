Mole
====

A lightweight python wsgi web framework

概述
=====
Mole 是纯python实现的一个极其轻量级的wsgi web框架。适合于喜欢DIY的人士，自己组装或者搭配web各个模块
(如：前端模板引擎，后端数据ORM，后端基础架子等等)，代码参考了开源项目Bottle，Mole虽小但“五脏俱全”，
本身不依赖于任何第三方模块。可以极其快捷和轻便地搭建一个demo Web系统。如果自己扩充功能模块再搭配其他
服务器后端(Nginx、Apache或uv-web)可以实现高性能应用。

特性
======
1. 轻量级，web基础东西都具备
2. 代码模块结构清晰,方便扩展或改造
3. 部署方便，只需将包放入能import的地方
4. 支持搭配各种前端模板引擎(如jinja2)

使用
======

from mole import route, run

@route('/')

def index():

    return 'Hello Mole!'


if __name__  == "__main__":

    run(host='localhost', port=8080)


部署在uv-web运行的方法

if __name__  == "__main__":

    run(server='uvweb',host='0.0.0.0', port=8033)

案例
======
[PyRedisAdmin](https://github.com/JoneXiong/PyRedisAdmin),一个用于在线查看和管理Redis数据的web应用

计划
======
1. 下一步将加入Websocket支持和方便的扩展接口
2. 加入一个扩展框架模块SpeedReport 用于快捷的数据查询和报表构建
