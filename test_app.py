# -*- coding: utf-8 -*-
from mole import route, run, static_file, error,get, post, put, delete, Mole
from mole.template import template
from mole import request
from mole import response
    
@route('/visit')
def visit():
    if request.get_cookie("visited"):
        return "Welcome back! Nice to see you again"
    else:
        response.set_cookie("visited", "yes")
        return "Hello there! Nice to meet you"
    
@route('/')
def index():
    return 'Hello Mole!'

if __name__  == "__main__":
    run(host='localhost', port=8080, reloader=True)
