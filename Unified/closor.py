#!/usr/bin/env python
from assignSession import *
from utils import componentInfo, sendEmail, setDatasetStatus, unifiedConfiguration, workflowInfo, siteInfo, sendLog, reqmgr_url, monitor_dir, duplicateLock, userLock, global_SI
import reqMgrClient
import json
import time
import sys
import os
from utils import getDatasetEventsAndLumis, campaignInfo, getDatasetPresence, findLateFiles, updateSubscription, makeReplicaRequest
from htmlor import htmlor
from collections import defaultdict
import reqMgrClient
import re
import copy
import random
import optparse

def spawn_harvesting(url, wfi , in_full):
    #SI = siteInfo()
    SI = global_SI()

    all_OK = {}
    requests = []
    outputs = wfi.request['OutputDatasets'] 
    if ('EnableHarvesting' in wfi.request and not wfi.request['EnableHarvesting']) and ('DQMConfigCacheID' in wfi.request and wfi.request['DQMConfigCacheID']):
        if not 'MergedLFNBase' in wfi.request:
            print "fucked up"
            sendEmail('screwed up wl cache','%s wl cache is bad'%(wfi.request['RequestName']))
            all_OK['fake'] = False
            return all_OK,requests

        wfi = workflowInfo(url, wfi.request['RequestName'])
        dqms = [out for out in outputs if '/DQM' in out]
        if not all([in_full[dqm_input] for dqm_input in dqms]):
            wfi.sendLog('closor',"will not be able to assign the harvesting: holding up")
            for dqm_input in dqms:
                all_OK[dqm_input] = False
                ## raise the subscription to high priority
                sites = set(wfi.request['NonCustodialSites'])
                for site in sites:
                    res = updateSubscription(url, site, dqm_input, priority='high')
                    print "increased priority",res
                return all_OK,requests

        for dqm_input in dqms:
            ## handle it properly
            harvesting_schema = {
                'Requestor': os.getenv('USER'),
                'RequestType' : 'DQMHarvest',
                'Group' : 'DATAOPS'
                }
            copy_over = [
                'AcquisitionEra',
                'ProcessingString',
                'DQMUploadUrl',
                'CMSSWVersion',
                'CouchDBName',
                'CouchWorkloadDBName',
                'CouchURL',
                'DbsUrl',
                'inputMode',
                'DQMConfigCacheID',
                'OpenRunningTimeout',
                'ScramArch',
                'CMSSWVersion',
                'Campaign',
                'Memory', #dummy
                'SizePerEvent', #dummy
                'GlobalTag', #dummy
                ]
            for item in copy_over:
                if item in wfi.request:
                    harvesting_schema[item] = copy.deepcopy(wfi.request[item])
                else:
                    print item,"is not in initial schema"

            harvesting_schema['InputDataset'] = dqm_input
            harvesting_schema['TimePerEvent'] = 1
            harvesting_schema['PrepID'] = 'Harvest-'+wfi.request['PrepID']
            if len(wfi.request['RequestString'])>60:
                wfi.request['RequestString']= wfi.request['RequestString'][:60]
                print "truncating request string",wfi.request['RequestString']
                
            harvesting_schema['RequestString'] = 'HARVEST-'+wfi.request['RequestString']
            harvesting_schema['DQMHarvestUnit'] = 'byRun'
            harvesting_schema['ConfigCacheUrl'] = harvesting_schema['CouchURL'] ## uhm, how stupid is that ?
            harvesting_schema['RequestPriority'] = min(wfi.request['RequestPriority']*10,999999)

            harvest_request = reqMgrClient.submitWorkflow(url, harvesting_schema)
            if not harvest_request:
                print "Error in making harvesting for",wfi.request['RequestName']
                print "schema"
                print json.dumps( harvesting_schema, indent = 2)
                harvest_request = reqMgrClient.submitWorkflow(url, harvesting_schema)
                if not harvest_request:
                    print "Error twice in harvesting for",wfi.request['RequestName']
                    print "schema"
                    print json.dumps( harvesting_schema, indent = 2)

            if harvest_request:
                requests.append( harvest_request )
                ## should we protect for setting approved ? no, it's notified below, assignment will fail, likely
                data = reqMgrClient.setWorkflowApproved(url, harvest_request)
                print "created",harvest_request,"for harvesting of",dqm_input
                wfi.sendLog('closor',"created %s for harvesting of %s"%( harvest_request, dqm_input))
                ## assign it directly
                team = wfi.request['Teams'][0]
                parameters={
                    'SiteWhitelist' : [SI.SE_to_CE(se) for se in wfi.request['NonCustodialSites']],
                    'AcquisitionEra' : wfi.acquisitionEra(),
                    'ProcessingString' : wfi.processingString(),
                    'MergedLFNBase' : wfi.request['MergedLFNBase'], 
                    'ProcessingVersion' : wfi.request['ProcessingVersion'],
                    'execute' : True
                    }
                if in_full[dqm_input]:
                    print "using full copy at",in_full[dqm_input]
                    parameters['SiteWhitelist'] = [SI.SE_to_CE(se) for se in in_full[dqm_input]]
                else:
                    print "cannot do anything if not having a full copy somewhere"
                    
                    all_OK[dqm_input]=False
                    continue

                result = reqMgrClient.assignWorkflow(url, harvest_request, team, parameters)
                if not result:
                    #sendEmail('harvesting request created','%s was created at announcement of %s in %s, failed to assign'%(harvest_request, dqm_input, wfi.request['RequestName']), destination=[wfi.request['Requestor']+'@cern.ch'])
                    wfi.sendLog('closor','%s was created at announcement of %s in %s, failed to assign'%(harvest_request, dqm_input, wfi.request['RequestName']))
                    sendLog('closor','%s was created at announcement of %s in %s, failed to assign'%(harvest_request, dqm_input, wfi.request['RequestName']), level='critical')
                else:
                    #sendEmail('harvesting request assigned','%s was created at announcement of %s in %s, and assigned'%(harvest_request, dqm_input, wfi.request['RequestName']), destination=[wfi.request['Requestor']+'@cern.ch']) 
                    wfi.sendLog('closor','%s was created at announcement of %s in %s, and assigned'%(harvest_request, dqm_input, wfi.request['RequestName']))

            else:
                #print "could not make the harvesting for",wfo.name,"not announcing"
                wfi.sendLog('closor',"could not make the harvesting request")
                sendLog('closor',"could not make the harvesting request for %s"% wfi.request['RequestName'], level='critical')
                all_OK[dqm_input]=False                    
    return (all_OK, requests)

def closor(url, specific=None, options=None):
    if userLock(): return
    if duplicateLock(): return
    if not componentInfo().check(): return


    UC = unifiedConfiguration()
    CI = campaignInfo()

    all_late_files = []
    check_fullcopy_to_announce = UC.get('check_fullcopy_to_announce')
    ## manually closed-out workflows should get to close with checkor
    if specific:
        wfs = session.query(Workflow).filter(Workflow.status=='close').filter(Workflow.name.contains(specific)).all()
    else:
        wfs = session.query(Workflow).filter(Workflow.status=='close').all()

    held = set()

    print len(wfs),"closing"
    max_per_round = UC.get('max_per_round').get('closor',None)
    if options.limit: max_per_round = options.limit
    random.shuffle( wfs )    
    if max_per_round: wfs = wfs[:max_per_round]

    batches = json.loads(open('batches.json').read())
    by_batch = {}
    batch_semaphore = defaultdict(int)
    for bname in batches:
        for wf in batches[bname]:
            by_batch[wf] = bname

    ## first go over everything and give it an extra go from batch
    for wfo in wfs:
        ## we want all the wf of the same batch to be in the close status before giving it a full go
        if wfo.name in by_batch:
            batch_semaphore[by_batch[wfo.name]] += 1
            
    batch_go = dict([(batch_name, len(batch_content)== batch_semaphore[batch_name]) for batch_name,batch_content in batches.items()])
    batch_warnings = defaultdict(set)
    batch_goodness = UC.get("batch_goodness")

    for wfo in wfs:

        if specific and not specific in wfo.name: continue

        ## what is the expected #lumis 
        wfi = workflowInfo(url, wfo.name )
        wfo.wm_status = wfi.request['RequestStatus']

        has_batch_go = False
        if wfo.name in by_batch and not batch_go[by_batch[wfo.name]]: 
            wfi.sendLog('closor', 'Cannot close for now because the batch %s is not all close'% by_batch[wfo.name])
            continue


        if wfi.request['RequestStatus'] in  ['announced','normal-archived']:
            ## manually announced ??
            wfo.status = 'done'
            wfo.wm_status = wfi.request['RequestStatus']
            wfi.sendLog('closor','%s is announced already : %s'%( wfo.name,wfo.wm_status))
        session.commit()

        expected_lumis = 1
        if not 'TotalInputLumis' in wfi.request:
            print wfo.name,"has not been assigned yet, or the database is corrupted"
        elif wfi.request['TotalInputLumis']==0:
            print wfo.name,"is corrupted with 0 expected lumis"
        else:
            expected_lumis = wfi.request['TotalInputLumis']

        ## what are the outputs
        outputs = wfi.request['OutputDatasets']
        ## check whether the number of lumis is as expected for each
        all_OK = defaultdict(lambda : False)
        #print outputs
        if len(outputs): 
            print wfo.name,wfi.request['RequestStatus']
        for out in outputs:
            event_count,lumi_count = getDatasetEventsAndLumis(dataset=out)
            odb = session.query(Output).filter(Output.datasetname==out).first()
            if not odb:
                print "adding an output object",out
                odb = Output( datasetname = out )
                odb.workflow = wfo
                session.add( odb )
            odb.nlumis = lumi_count
            odb.nevents = event_count
            odb.workfow_id = wfo.id
            if odb.expectedlumis < expected_lumis:
                odb.expectedlumis = expected_lumis
            else:
                expected_lumis = odb.expectedlumis
            odb.date = time.mktime(time.gmtime())
            session.commit()
            fraction = lumi_count/float(expected_lumis)*100.

            completion_line = "%60s %d/%d = %3.2f%%"%(out,lumi_count,expected_lumis,fraction)
            wfi.sendLog('closor',"\t%s"% completion_line)
            if wfi.isRelval() and fraction < batch_goodness:
                batch_warnings[ wfi.getCampaign()].add( completion_line )

            all_OK[out] = True 


        ## check for at least one full copy prior to moving on
        in_full = {}
        for out in outputs:
            in_full[out] = []
            presence = getDatasetPresence( url, out )
            where = [site for site,info in presence.items() if info[0]]
            if where:
                all_OK[out] = True
                print out,"is in full at",",".join(where)
                in_full[out] = copy.deepcopy(where)
            else:

                going_to = wfi.request['NonCustodialSites']+wfi.request['CustodialSites']
                wfi.sendLog('closor',"%s is not in full anywhere. send to %s"%(out, ",".join(sorted(going_to))))
                at_destination = dict([(k,v) for (k,v) in presence.items() if k in going_to])
                else_where = dict([(k,v) for (k,v) in presence.items() if not k in going_to])
                print json.dumps( at_destination )
                print json.dumps( else_where, indent=2 )
                ## do the full stuck transfer study, missing files and shit !
                for there in going_to:
                    late_info = findLateFiles(url, out, going_to = there )
                    for l in late_info:
                        l.update({"workflow":wfo.name,"dataset":out})
                    all_late_files.extend( late_info )
                if check_fullcopy_to_announce:
                    ## only set this false if the check is relevant
                    all_OK[out] = False

    
        ## verify if we have to do harvesting

        if not options.no_harvest:
            (OK, requests) = spawn_harvesting(url, wfi, in_full)
            all_OK.update( OK )

        ## only that status can let me go into announced
        if all(all_OK.values()) and wfi.request['RequestStatus'] in ['closed-out']:
            print wfo.name,"to be announced"
            results=[]#'dummy']
            if not results:
                for out in outputs:
                    _,dsn,process_string,tier = out.split('/')
                    #tier = out.split('/')[-1]
                    #process_string = out.split(',')[-2]
                    #dsn = out.split(',')[-3]
                    if all_OK[out]:
                        results.append(setDatasetStatus(out, 'VALID'))
                    if all_OK[out] and wfi.isRelval():
                        ## make the specific relval rules and the replicas
                        ## figure the destination(s) out
                        destinations = set()
                        if tier != "RECO" and tier != "ALCARECO":
                            destinations.add('T2_CH_CERN')
                        if tier == "GEN-SIM":
                            destinations.add('T1_US_FNAL_Disk')
                        if tier == "GEN-SIM-DIGI-RAW":
                            destinations.add('T1_US_FNAL_Disk')
                        if tier == "GEN-SIM-RECO":
                            destinations.add('T1_US_FNAL_Disk')

                        if "RelValTTBar" in dsn and "TkAlMinBias" in process_string and tier != "ALCARECO":
                            destinations.add('T2_CH_CERN')

                        if "MinimumBias" in dsn and "SiStripCalMinBias" in process_string and tier != "ALCARECO":
                            destinations.add('T2_CH_CERN')
                        
                        if destinations:
                            wfi.sendLog('closor', '%s to go to %s'%(out, ', '.join( sorted( destinations ))))

                        ## call to makereplicarequest under relval => done
                        for site in destinations:
                            result = makeReplicaRequest(url, site, [out], 'Copy for release validation consumption', priority='high', approve=True, mail=False, group='RelVal')
                            try:
                                request_id =  result['phedex']['request_created'][0]['id']
                                results.append( True )
                            except:
                                results.append( 'Failed relval transfer' )
                        
                    elif all_OK[out]:

                        campaign = None
                        try:
                            campaign = out.split('/')[2].split('-')[0]
                        except:
                            if 'Campaign' in wfi.request and wfi.request['Campaign']:
                                campaign = wfi.request['Campaign']
                        to_DDM = False
                        ## campaign override
                        if campaign and campaign in CI.campaigns and 'toDDM' in CI.campaigns[campaign] and tier in CI.campaigns[campaign]['toDDM']:
                            to_DDM = True

                        ## by typical enabling
                        if tier in UC.get("tiers_to_DDM"):
                            to_DDM = True
                        ## check for unitarity
                        if not tier in UC.get("tiers_no_DDM")+UC.get("tiers_to_DDM"):
                            print "tier",tier,"neither TO or NO DDM for",out
                            results.append('Not recognitized tier %s'%tier)
                            #sendEmail("failed DDM injection","could not recognize %s for injecting in DDM"% out)
                            sendLog('closor', "could not recognize %s for injecting in DDM"% out, level='critical')
                            continue

                        n_copies = 2
                        destinations=[]
                        if to_DDM and campaign and campaign in CI.campaigns and 'DDMcopies' in CI.campaigns[campaign]:
                            ddm_instructions = CI.campaigns[campaign]['DDMcopies']
                            if type(ddm_instructions) == int:
                                n_copies = CI.campaigns[campaign]['DDMcopies']
                            elif type(ddm_instructions) == dict:
                                ## a more fancy configuration
                                for ddmtier,indication in ddm_instructions.items():
                                    if ddmtier==tier or ddmtier in ['*','all']:
                                        ## this is for us
                                        if 'N' in indication:
                                            n_copies = indication['N']
                                        if 'host' in indication:
                                            destinations = indication['host']
                                            
                        destination_spec = ""
                        if destinations:
                            destination_spec = "--destination="+",".join( destinations )
                        group_spec = "" ## not used yet 
                        ### should make this a campaign configuration
                        ## inject to DDM when necessary
                        if to_DDM:
                            print "Sending",out," to DDM"
                            p = os.popen('python assignDatasetToSite.py --nCopies=%d --dataset=%s %s %s --debug 0 --exec'%(n_copies, out,destination_spec, group_spec))
                            ddm_text = p.read()
                            print ddm_text
                            status = p.close()
                            if status!=None:
                                print "Failed DDM, retrying to send",out,"a second time"
                                p = os.popen('python assignDatasetToSite.py --nCopies=%d --dataset=%s %s %s --debug 1 --exec'%(n_copies, out,destination_spec, group_spec))

                                ddm_text = p.read()
                                print ddm_text
                                status = p.close()    
                                if status!=None:
                                    #sendEmail("failed DDM injection","could not add "+out+" to DDM pool. check closor logs.")
                                    sendLog('closor',"could not add "+out+" to DDM pool. check closor logs.", level='critical')
                                    if options.force: status = True
                            results.append( status )
                            if status == None:
                                wfi.sendLog('closor',ddm_text)
                                wfi.sendLog('closor','%s is send to AnalysisOps DDM pool in %s copies %s'%( out, n_copies, destination_spec))
                                                            
                    else:
                        print wfo.name,"no stats for announcing",out
                        results.append('No Stats')

                if all(map(lambda result : result in ['None',None,True],results)):
                    ## only announce if all previous are fine
                    res = reqMgrClient.announceWorkflowCascade(url, wfo.name)
                    if not res in ['None',None]:
                        ## check the status again, it might well have toggled
                        wl_bis = workflowInfo(url, wfo.name)
                        wfo.wm_status = wl_bis.request['RequestStatus']
                        session.commit()
                        if wl_bis.request['RequestStatus'] in  ['announced','normal-archived']:
                            res = None
                        else:
                            ## retry ?
                            res = reqMgrClient.announceWorkflowCascade(url, wfo.name) 
                            
                    results.append( res )
                                
            #print results
            if all(map(lambda result : result in ['None',None,True],results)):
                wfo.status = 'done'
                session.commit()
                wfi.sendLog('closor',"workflow is announced")
            else:
                wfi.sendLog('closor',"Error with %s to be announced \n%s"%( wfo.name, json.dumps( results )))
                
        else:
            print wfo.name,"not good for announcing:",wfi.request['RequestStatus']
            wfi.sendLog('closor',"cannot be announced")
            held.add( wfo.name )

    days_late = 0.
    retries_late = 10

    really_late_files = [info for info in all_late_files if info['retries']>=retries_late]
    really_late_files = [info for info in really_late_files if info['delay']/(60*60*24.)>=days_late]

    if really_late_files:
        subject = 'These %d files are lagging for %d days and %d retries announcing dataset \n%s'%(len(really_late_files), days_late, retries_late, json.dumps( really_late_files , indent=2) )
        #sendEmail('waiting for files to announce', subject)
        sendLog('closor', subject, level='warning')
        sendLog('closor',subject)
        print subject
        open('%s/stuck_files.json'%monitor_dir,'w').write( json.dumps( really_late_files , indent=2))

    if held:
        sendLog('closor',"the workflows below are held up \n%s"%("\n".join( sorted(held) )), level='critical')

    for bname,go in batch_go.items():
        if go:
            subject = "Release Validation Samples Batch %s"% bname
            issues=""
            if batch_warnings[ bname ]:
                issues="The following datasets have outstanding completion (<%d%%) issues:\n\n"% batch_goodness
                issues+="\n".join( sorted( batch_warnings[ bname ] ))
                issues+="\n\n"
            text = """
Dear all,

a batch of release validation workflows has finished.

Batch ID:

%s

Detail of the workflows

https://dmytro.web.cern.ch/dmytro/cmsprodmon/requests.php?campaign=%s

%s 
This is an automated message.
"""%( bname, 
      bname,
      issues)
            to = ['hn-cms-relval@cern.ch']
            sendEmail(subject, text, to )
            



    
if __name__ == "__main__":
    url = reqmgr_url
    parser = optparse.OptionParser()
    parser.add_option('--no_harvest',help='Bypass the harvesting',default=False,action='store_true')
    parser.add_option('--limit',help="Number of workflow to pass",default=0, type=int)
    parser.add_option('--force', help="Force pushing the workflow through", default=False,action='store_true')
    (options,args) = parser.parse_args()

    spec=None
    if len(args)!=0:
        spec = args[0]

    closor(url,spec, options=options)

    if (not spec) and (not options.limit):
        htmlor()
