#!/usr/bin/env python3
"""Compare client-ban isolation with disposable KVM pods; preserve application state."""
import copy,json,os,time
import workloads as w
prefix='ip-ban-check-'+str(os.getpid())
created=[]
def query(url):
 code='import urllib.request,urllib.error\ntry:\n r=urllib.request.urlopen('+repr(url)+',timeout=3);print(r.status);r.read()\nexcept urllib.error.HTTPError as e:print(e.code)\nexcept OSError:print(0)'
 import shlex
 return int(w.vm.ssh('python3 -c '+shlex.quote(code),timeout=8).stdout.strip())
try:
 template=w.kube_json('-n',w.NAMESPACE,'get','deploy','frontend')['spec']['template']
 targets=[]
 for mode in ['http','tcp']:
  name=prefix+'-'+mode
  assert not w.kube('-n',w.NAMESPACE,'get','pod,svc',name,'--ignore-not-found','-o','name').stdout.strip()
  pod={'apiVersion':'v1','kind':'Pod','metadata':{'name':name,'namespace':w.NAMESPACE,'labels':{'msc128-ban-isolation':mode}},'spec':copy.deepcopy(template['spec'])}
  for volume in pod['spec']['volumes']:
   if volume['name']=='nginx-state':volume.pop('persistentVolumeClaim');volume['emptyDir']={}
  container=pod['spec']['containers'][0]
  container['readinessProbe']={'periodSeconds':3, **({'tcpSocket':{'port':8080}} if mode=='tcp' else {'httpGet':{'path':'/index.html','port':8080}})}
  assert container['readinessProbe'].get('tcpSocket' if mode=='tcp' else 'httpGet')
  result=json.loads(w.kube('create','-f','-','-o','json',data=json.dumps(pod)).stdout);created.append(('pod',name,result['metadata']['uid']))
  svc={'apiVersion':'v1','kind':'Service','metadata':{'name':name,'namespace':w.NAMESPACE},'spec':{'selector':{'msc128-ban-isolation':mode},'ports':[{'port':8080,'targetPort':8080}]}}
  result=json.loads(w.kube('create','-f','-','-o','json',data=json.dumps(svc)).stdout);created.append(('svc',name,result['metadata']['uid']));targets.append((mode,name,result['spec']['clusterIP']))
 for mode,name,service_ip in targets:
  w.kube('-n',w.NAMESPACE,'wait','pod/'+name,'--for=condition=Ready','--timeout=60s',timeout=70)
  pod_ip=w.kube_json('-n',w.NAMESPACE,'get','pod',name)['status']['podIP']
  assert query('http://'+service_ip+':8080/entry')==200
  code='import urllib.request,urllib.error\nfor i in range(100):\n try: urllib.request.urlopen("http://'+pod_ip+':8080/msc128-invalid-url-"+str(i),timeout=3).read()\n except urllib.error.HTTPError as e: e.read()\nprint("100 invalid URLs sent to isolated '+mode+' pod")'
  import shlex
  print(w.vm.ssh('python3 -c '+shlex.quote(code),timeout=30).stdout.strip(),flush=True)
  assert query('http://'+pod_ip+':8080/entry')==403
 time.sleep(10)
 results=[]
 for mode,name,service_ip in targets:
  pod=w.kube_json('-n',w.NAMESPACE,'get','pod',name)
  ready=next(x['status']=='True' for x in pod['status']['conditions'] if x['type']=='Ready')
  service_status=query('http://'+service_ip+':8080/entry')
  other=w.kube('-n',w.NAMESPACE,'exec','deploy/frontend','--','wget','-qO-','-T','3','http://'+service_ip+':8080/entry',check=False)
  other_ok=other.returncode==0 and '<html' in other.stdout
  results.append({'mode':mode,'ready':ready,'blockedClientResponse':service_status,'otherClientAvailable':other_ok})
 assert results[0]=={'mode':'http','ready':False,'blockedClientResponse':0,'otherClientAvailable':False},results
 assert results[1]=={'mode':'tcp','ready':True,'blockedClientResponse':403,'otherClientAvailable':True},results
 print(json.dumps(results),flush=True)
finally:
 for kind,name,uid in reversed(created):
  obj=w.kube_json('-n',w.NAMESPACE,'get',kind,name);assert obj['metadata']['uid']==uid
  w.kube('-n',w.NAMESPACE,'delete',kind,name,'--wait=true',timeout=60)
