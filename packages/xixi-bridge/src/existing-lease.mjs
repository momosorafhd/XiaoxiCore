import {mkdir,open,lstat,chmod} from 'node:fs/promises';
import {constants} from 'node:fs';
import {resolve,join} from 'node:path';
export async function acquireExistingDataLease(directory,tryLockExclusive){
  if(process.platform!=='linux'&&process.platform!=='darwin')throw Error('EXISTING_BRIDGE_REQUIRES_POSIX_HOST');
  const root=resolve(directory);await mkdir(root,{recursive:true,mode:0o700});
  const info=await lstat(root);
  if(!info.isDirectory()||info.isSymbolicLink()||info.uid!==process.getuid())throw Error('BRIDGE_DATA_MUST_BE_OWNED_DIRECTORY');
  await chmod(root,0o700);
  const lockPath=join(root,'existing-host.lock');
  const fd=await open(lockPath,constants.O_CREAT|constants.O_RDWR|constants.O_NOFOLLOW,0o600);
  try{
    await fd.chmod(0o600);await tryLockExclusive(fd.fd);
    const held=await fd.stat({bigint:true}),actual=await lstat(lockPath,{bigint:true});
    if(!actual.isFile()||actual.isSymbolicLink()||held.dev!==actual.dev||held.ino!==actual.ino)throw Error('BRIDGE_LOCK_PATH_CHANGED');
    return {root,release:()=>fd.close()};
  }catch(e){await fd.close();throw e;}
}
