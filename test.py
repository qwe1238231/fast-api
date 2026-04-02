# 實驗 1
x = 100
print(id(x))
x += 1
print(id(x))  # 變了還是沒變？

# 實驗 2
d = {"name": "Alice"}
print(id(d))
d["age"] = 30
print(id(d))  # 變了還是沒變？

# 實驗 3
t = (1, 2, 3)
print(id(t))
t = t + (4,)
print(id(t))  # 變了還是沒變？