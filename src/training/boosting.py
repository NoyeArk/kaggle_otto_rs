import gc
import cuml
import cudf
import glob
import numba
import optuna
import numpy as np
import pandas as pd

from cuml import PCA
from numerize.numerize import numerize
from utils.torch import seed_everything
from sklearn.metrics import roc_auc_score

from model_zoo import TRAIN_FCTS, OBJECTIVE_FCTS
from model_zoo.xgb import objective_xgb
from inference.predict import predict_batched
from utils.load import load_parquets_cudf_folds
from utils.metrics import evaluate
from utils.plot import plot_importances


def optimize(df_train, df_val, regex, config, log_folder, n_trials=100, fold=0, debug=False, run=None):
    print(f"\n-------------  Optimizing {config.model.upper()} Model  -------------\n")
    seed_everything(config.seed)

    print(f"\n    -> {numerize(len(df_train))} training candidates")
    print(f"    -> {numerize(len(df_val))} validation candidates\n")

    study = optuna.create_study(direction="maximize")

    objective_fct = OBJECTIVE_FCTS[config.model]
    objective = lambda x: objective_fct(
        x,
        df_train,
        df_val,
        regex,
        features=config.features,
        target=config.target,
        params=config.params,
        folds_file=config.folds_file,
        probs_file=config.probs_file,
        probs_mode=config.probs_mode,
        fold=fold,
        debug=debug,
        no_tqdm=log_folder is not None,
        run=run,
    )

    study.optimize(objective, n_trials=1 if debug else n_trials)

    print("Final params :\n", study.best_params)
    return study


def train(df_train, df_val, regex, config, log_folder=None, fold=0, debug=False):
    print(f"\n-------------  Training {config.model.upper()} Model  -------------\n")

    print(f"    -> {numerize(len(df_train))} training candidates")
    print(f"    -> {numerize(len(df_val))} validation candidates\n")
    
    train_fct = TRAIN_FCTS[config.model]
    df_val, model = train_fct(
        df_train,
        df_val,
        regex,
        features=config.features,
        target=config.target,
        params=config.params,
        use_es=config.use_es,
        num_boost_round=config.num_boost_round,
        folds_file=config.folds_file,
        probs_file=config.probs_file,
        probs_mode=config.probs_mode,
        fold=fold,
        debug=debug,
        no_tqdm=log_folder is not None,
    )

    # Feature importance
    if config.model == "xgb":
        ft_imp = model.get_score()
    else:
        ft_imp = model.feature_importances_  # TODO
    try:
        ft_imp = pd.DataFrame(
            pd.Series(ft_imp, index=config.features), columns=["importance"]
        )
    except:
        ft_imp = None
        
    if config.mode == "test":
        return df_val, ft_imp

    if log_folder is None:
        return df_val, ft_imp

    # Save model
    if config.model == "xgb":
        model.save_model(log_folder + f"{config.model}_{fold}.json")
    elif config.model == "lgbm":
        try:
            model.booster_.save_model(log_folder + f"{config.model}_{fold}.txt")
        except Exception:
            model.save_model(log_folder + f"{config.model}_{fold}.txt")
    else:   # catboost, verif
        model.save_model(log_folder + f"{config.model}_{fold}.txt")

    return df_val, ft_imp


def kfold(regex, test_regex, config, log_folder, debug=False, run=None):
    seed_everything(config.seed)
    ft_imps, scores = [], []

    for fold in range(config.k):
        if fold not in config.selected_folds:
            continue

        print(f"\n=============   Fold {fold + 1} / {config.k}   =============\n")
        seed_everything(config.seed + fold)

        df_train, df_val = load_parquets_cudf_folds(
            regex,
            config.folds_file,
            fold=fold,
            pos_ratio=config.pos_ratio,
            target=config.target,
            use_gt=config.use_gt_sessions,
            use_gt_for_val=True,
            columns=['session', 'candidates', 'gt_clicks', 'gt_carts', 'gt_orders'] + config.features,
            max_n=5 if debug else 0,
            probs_file=config.probs_file if config.restrict_all else "",
            probs_mode=config.probs_mode if config.restrict_all else "",
            seed=config.seed,
            no_tqdm=log_folder is not None
        )

        if config.use_extra:
            df_extra = load_parquets_cudf_folds(
                config.extra_regex,
                pos_ratio=config.pos_ratio,
                target=config.target,
                use_gt=True,
                train_only=True,
                columns=['session', 'candidates', 'gt_clicks', 'gt_carts', 'gt_orders'] + config.features,
                max_n=1 if debug else 0,
                seed=config.seed,
                no_tqdm=log_folder is not None
            )
            if config.extra_prop:
                df_extra = df_extra.sample(int(config.extra_prop * len(df_train)))
            print(f'Using {len(df_extra)} extra samples')
            df_train = pd.concat([df_train, df_extra], ignore_index=True)

        if config.model == "lgbm":
            df_train['has_gt'] = df_train.groupby('session')[config.target].transform("max")
            df_train = df_train[df_train['has_gt'] == 1].drop('has_gt', axis=1).reset_index(drop=True)

            assert len(df_val['session'].unique()) == len(df_val[df_val["session"] != df_val["session"].shift(1).fillna('')])
            assert len(df_train['session'].unique()) == len(df_train[df_train["session"] != df_train["session"].shift(1).fillna('')])

        if fold in config.folds_optimize:
            study = optimize(
                df_train,
                df_val,
                regex,
                config,
                log_folder,
                n_trials=1 if debug else config.n_trials,
                debug=debug,
                fold=fold,
                run=run,
            )
            config.params.update(study.best_params)
            
            if run is not None:
                run[f"fold_{fold}/best_params/"] = study.best_params
            
        df_val, ft_imp = train(
            df_train,
            df_val,
            regex,
            config,
            log_folder=log_folder,
            fold=fold,
            debug=debug
        )
        ft_imps.append(ft_imp)
        
        try:
            train_sessions = set(list(df_train["session"].unique()))
            val_sessions = set(list(df_val["session"].unique().to_pandas()))
            print('Train / val session inter', len(train_sessions.intersection(val_sessions)))
        except:
            pass
        
        if log_folder is None:
            return ft_imp

        if run is not None:
            score = evaluate(df_val, config.target, verbose=0)
            scores.append(score)
            run[f"fold_{fold}/recall"] = score
        
        print('\n -> Saving val predictions \n')
        df_val[['session', 'candidates', 'pred']].to_parquet(log_folder + f"df_val_{fold}.parquet")

        del df_train, df_val, ft_imp
        numba.cuda.current_context().deallocations.clear()
        gc.collect()
                
        if config.model == "xgb":
            model = cuml.ForestInference.load(
                filename=log_folder + f"xgb_{fold}.json",
                model_type='xgboost_json',
            )
        else:
            model = cuml.ForestInference.load(
                filename=log_folder + f"lgbm_{fold}.txt",
                model_type='lightgbm',
            )

        df_test = predict_batched(
            model,
            test_regex,
            config.features,
            debug=debug,
            probs_file=config.probs_file if config.restrict_all else "",
            probs_mode=config.probs_mode if config.restrict_all else "",
            ranker=("rank" in config.params.get("objective", "")),
            no_tqdm=True,
        )
        
        print('\n -> Saving test predictions \n')
        df_test[['session', 'candidates']] = df_test[['session', 'candidates']].astype('int32')
        df_test['pred'] = df_test['pred'].astype('float32')
        df_test[['session', 'candidates', 'pred']].to_parquet(log_folder + f"df_test_{fold}.parquet")

        del df_test, model
        numba.cuda.current_context().deallocations.clear()
        gc.collect()

    ft_imps = pd.concat(ft_imps).reset_index().groupby('index').mean()
    if log_folder is not None:
        ft_imps.to_csv(log_folder + "ft_imp.csv")

    if run is not None:
        run["global/logs"].upload(log_folder + "logs.txt")
        run["global/recall"] = np.mean(scores)
        plot_importances(ft_imps, run=run)

    return ft_imps
