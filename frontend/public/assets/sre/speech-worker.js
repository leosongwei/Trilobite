// stub – speech worker not used
self.postMessage({cmd:"Ready",data:{}});
self.onmessage=function(e){self.postMessage({cmd:"Finished",data:{success:true,result:"{}"}})};
