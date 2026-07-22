我们来造一个简单的coding agent。

# 风格和注意事项

## 后端

* 3.3后的现代python不应该用__init__.py
* 全篇采用绝对引用`from src.trilobite.xxxx import yyyy`，禁止使用相对引用

## 前端

* 不要去整个读取那些编译出来的js文件，超级大，会耗尽上下文