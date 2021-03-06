# -*- coding: utf-8 -*-
import logging; logging.basicConfig(level=logging.INFO)
import asyncio, os, json, time
from datetime import datetime
from aiohttp import web
import orm
from jinja2 import Environment, FileSystemLoader
from coroweb import add_routes, add_static
from config import configs
from handlers import cookie2user, COOKIE_NAME

def init_jinja2(app, **kw):
	logging.info('Init jinja2...')
	options = dict(
		# 字典的get()方法返回指定键的值, 如果值不在字典中返回默认值
		autoescape = kw.get('autoescape', True),    # 自动转义
		block_start_string = kw.get('block_start_string', '{%'),
		block_end_string = kw.get('block_end_string', '%}'),
		variable_start_string = kw.get('variable_start_string', '{{'),
		variable_end_string = kw.get('variable_end_string', '}}'),
		auto_reload = kw.get('auto_reload', True)    # 自动重新加载模板
	)
	path = kw.get('path', None)
	if path is None:
		# __file__获取当前执行脚本
		path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')
	logging.info('Set jinja2 template path: %s' % path)
	# Environment(loader=PackageLoader('path'), 其他高级参数...)
	# 创建一个默认设定下的模板环境和一个在path目录下寻找模板的加载器
	env = Environment(loader=FileSystemLoader(path), **options)
	filters = kw.get('filters', None)
	if filters is not None:
		for name, f in filters.items():
			env.filters[name] = f
	app['__templating__'] = env

# middlewar把通用的功能从每个URL处理函数中拿出来, 集中放到一个地方
# 接受一个app实例, 一个handler(URL处理函数, 如index), 并返回一个新的handler
async def logger_factory(app, handler):
	async def logger(request):
		logging.info('Request: %s %s' % (request.method, request.path))
		return (await handler(request))
	return logger

async def auth_factory(app, handler):
	# 伴随每一个请求, aiohttp内部创建一个Request对象request
	# request = web_reqrep.Request(
    #     app, message, payload, self.transport, self.reader,
    #     self.writer, secure_proxy_ssl_header=self._secure_proxy_ssl_header)
	async def auth(request):
		logging.info('Check user: %s %s' % (request.method, request.path))
		request.__user__ = None
		# cookies是一个dict
		cookie_str = request.cookies.get(COOKIE_NAME)
		if cookie_str:
			# 解密cookie
			user = await cookie2user(cookie_str)
			if user:
				logging.info('Set current user: %s' % user.email)
				request.__user__ = user
		if request.path.startswith('/manage/') and (request.__user__ is None or not request.__user__.admin):	# /manage/?
			return web.HTTPFound('/signin')
		return (await handler(request))
	return auth

async def data_factory(app, handler):
	async def parse_data(request):
		if request.method == 'POST':
			if request.content_type.startswith('application/json'):
				request.__data__ = await request.json()
				logging.info('Request json: %s' % str(request.__data__))
			elif request.content_type.startswith('application/x-www-form-urlencoded'):
				request.__data__ = await request.post()
				logging.info('Request form: %s' % str(request.__data__))
		return (await handler(request))
	return parse_data

# 转化得到response对象的middleware
async def response_factory(app, handler):
	async def response(request):
		logging.info('Response handler...')
		r = await handler(request)
		# web.StreamResponse是HTTP响应处理的基类
		# 包含用于设置HTTP响应头，Cookie，响应状态码，写入HTTP响应BODY等的方法
		if isinstance(r, web.StreamResponse):
			return r
		if isinstance(r, bytes):
			# 转换为web.Response对象
			resp = web.Response(body=r)
			# .*（ 二进制流，不知道下载文件类型）
			resp.content_type = 'application/octet-stream'
			return resp
		if isinstance(r, str):
			if r.startswith('redirect:'):
				return web.HTTPFound(r[9:])
			resp = web.Response(body=r.encode('utf-8'))
			# .html
			resp.content_type = 'text/html;charset=utf-8'
			return resp
		if isinstance(r, dict):
			template = r.get('__template__')
			if template is None:
				resp = web.Response(body=json.dumps(r, ensure_ascii=False, default=lambda o: o.__dict__).encode('utf-8'))
				# 序列化后的JSON字符串
				resp.content_type = 'application/json;charset=utf-8'
				return resp
			else:
				r['__user__'] = request.__user__
				# 调用get_template()方法环境中加载模板，调用render()方法用若干变量来渲染它
				resp = web.Response(body=app['__templating__'].get_template(template).render(**r).encode('utf-8'))
				resp.content_type = 'text/html;charset=utf-8'
				return resp
		# HTTP状态码和响应头部
		if isinstance(r, int) and r >= 100 and r < 600:
			return web.Response(r)
		if isinstance(r, tuple) and len(r) == 2:
			t, m = r
			if isinstance(t, int) and t >= 100 and t < 600:
				return web.Response(t, str(m))
		resp = web.Response(body=str(r).encode('utf-8'))
		# .txt
		resp.content_type = 'text/plain;charset=utf-8'
		return resp
	return response

def datetime_filter(t):
	# time.time()返回当前时间的时间戳(1970纪元后经过的浮点秒数)
	delta = int(time.time() - t)
	if delta < 60:
		return u'1分钟前'
	if delta < 3600:
		return u'%s分钟前' % (delta // 60)
	if delta < 86400:
		return u'%s小时前' % (delta // 3600)
	if delta < 604800:
		return u'%s天前' % (delta // 86400)
	# 把timestamp转换为datetime
	dt = datetime.fromtimestamp(t)
	return u'%s年%s月%s日' % (dt.year, dt.month, dt.day)

# 协程，不能直接运行，需要把协程加入到事件循环(loop), 由后者在适当的时候调用
async def init(loop):
	await orm.create_pool(loop=loop, **configs.db)
	# 创建Web服务器，即aiohttp.web.Application类的实例，作用是处理URL、HTTP协议
	# 添加middleware时自动变成倒序
	app = web.Application(loop=loop, middlewares=[
		logger_factory, auth_factory, response_factory
		])
	init_jinja2(app, filters=dict(datetime=datetime_filter))    # 没有参数t?
	# import handlers.py
	add_routes(app, 'handlers')
	add_static(app)
	# 用协程创建TCP服务（这里写的是我的虚拟机地址，为了本机也能访问）
	srv = await loop.create_server(app.make_handler(), '0.0.0.0', 8000, reuse_address=True, reuse_port=True)
	logging.info('Server started at http://60.205.221.43:80...') 
	return srv

# 创建一个事件循环
loop = asyncio.get_event_loop()
# 将协程注册到事件循环，并启动事件循环
loop.run_until_complete(init(loop))
# run_forever会一直运行，直到stop在协程中被调用
loop.run_forever()
